"""Semantic embeddings lane: the calibration store becomes searchable.

The third model class under the same accountability machinery. sys1 judges
what the work SAYS, sdm1/tabular scores what the work MEASURES, and this lane
indexes what the work LOOKS LIKE: embeddings over the graded history so
near-dupe inputs, nearest graded neighbors, and gate shortlists stop being
exact-`input_sha256`-only.

Operating rule (2026-10-07 plan): EMBEDDINGS PROPOSE, SYS1/SDM1 DISPOSE.
Everything here is eval-only (tag `sem1_raw` in providers_used) — similarity
thresholds don't transfer and need their own calibration floors before any
verdict or join trusts them. Nothing in this module gates, blocks, or joins.

Batch-only rule: like the tabular lane, no command here may run inside a
synchronous hook path; hooks never reference this module (socket-guard test).

Recoverable text: the JSONL stores are redacted by design (no input text), so
indexing embeds what a row still references — plan files on disk, stored
claim lists, feedback notes — keyed by the row's `input_sha256`. Rows with no
recoverable text are counted and skipped, never guessed at.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from .config import EXIT_ERROR, EXIT_OK, EXIT_WARN, LOG_DIR, log_record
from .judgment import sem1

# section: semantics

VECTORS_DIR = Path.home() / ".local" / "share" / "dev-decisions" / "vectors"
FEEDBACK_FILE = LOG_DIR / "feedback" / "feedback.jsonl"
SEM1_RAW_TAG = "sem1_raw"          # providers_used tag until calibration floors exist
_DEDUP_THRESHOLD = 0.90            # EVAL-ONLY similarity cutoff (never a join key)
_NN_K = 5
_LOG_TEXT_CHARS = 8_000
_MODEL = "embeddinggemma-2-BF16"   # the local native llama-server model
_CALIBRATION_CORPUS = "calibration"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TEXT_EXTS = {".txt", ".md", ".json", ".jsonl", ".csv", ".py", ".js", ".mjs",
              ".ts", ".toml", ".yaml", ".yml", ".html", ".css"}


def corpus_root(corpus: str | None) -> Path:
    """Store root for a corpus. The default calibration corpus uses VECTORS_DIR
    itself (byte-identical to the pre-corpus layout); a named corpus gets
    VECTORS_DIR/<name>/. A corpus name arrives as argv and becomes a path, so
    anything that is not a filesystem slug is refused before it can traverse."""
    if not corpus or corpus == _CALIBRATION_CORPUS:
        return VECTORS_DIR
    if not re.fullmatch(r"[A-Za-z0-9._-]+", corpus) or corpus.startswith("."):
        print(f"error: bad corpus name {corpus!r} — use [A-Za-z0-9._-] without a leading dot",
              file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    return VECTORS_DIR / corpus


def collect_input_files(root: Path) -> dict:
    """Files under a directory partitioned by modality, extension-routed:
    images → st-worker, known-text extensions → llama-server; anything else is
    counted and skipped, never guessed at. Sorted for determinism. Pure."""
    text: list[Path] = []
    image: list[Path] = []
    skipped = 0
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in _IMAGE_EXTS:
            image.append(p)
        elif ext in _TEXT_EXTS:
            text.append(p)
        else:
            skipped += 1
    return {"text": text, "image": image, "skipped": skipped}


def merge_entries(existing: dict | None, new_entries: list[dict]) -> list[dict]:
    """Existing manifest entries + new entries, dedup by key, first wins — an
    --inputs index accumulates across runs and stays idempotent per content
    sha (rebuild() dedups again below us; this keeps the vector mapping honest
    before it). Pure."""
    out: list[dict] = []
    seen: set[str] = set()
    for e in list((existing or {}).get("entries") or []) + list(new_entries):
        key = e.get("key")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def sem1_ready() -> tuple[bool, str]:
    """(ready, reason) — library importable and a provider registered."""
    if sem1 is None:
        return False, "sem1 not importable — set DEV_DECISIONS_SEM1_PATH or install ~/Projects/sem1"
    try:
        if "llama-server" in sem1.registry.all_ids():
            return True, "sem1 + llama-server available"
        return False, "sem1 predates the llama-server provider — update ~/Projects/sem1"
    except Exception as e:
        return False, f"sem1 registry unreadable: {e}"


# ── pure layer (fixture-tested; no I/O beyond what callers pass in) ─────────


def iter_log_rows(log_dir: Path | None = None):
    """Yield (path, record) for every parseable JSONL row in the event logs."""
    log_dir = Path(log_dir) if log_dir else LOG_DIR
    for path in sorted(log_dir.rglob("*.jsonl")):
        if "feedback" in path.parts:
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield path, rec


def recoverable_text(record: dict) -> str | None:
    """Text a row still references, or None. Never fabricates.

    Priority: a plan file that still exists > a stored claims list > a
    feedback note. Anything else has no recoverable text (the stores are
    redacted by design) and is skipped by the indexer.
    """
    plan = record.get("plan")
    if isinstance(plan, str) and plan:
        p = Path(plan)
        if p.exists() and p.is_file():
            try:
                return p.read_text()[:_LOG_TEXT_CHARS]
            except OSError:
                pass
    claims = record.get("claims")
    if isinstance(claims, list) and claims:
        joined = " ".join(str(c) for c in claims)
        if joined.strip():
            return joined[:_LOG_TEXT_CHARS]
    note = record.get("note")
    if isinstance(note, str) and note.strip():
        task = record.get("task", "")
        return f"[{task}] {note}"[:_LOG_TEXT_CHARS] if task else note[:_LOG_TEXT_CHARS]
    return None


def build_entries(rows) -> list[dict]:
    """rows: (path, record) pairs -> index entries with recoverable text.

    One entry per row that yields text; key = the row's input_sha256 (falls
    back to a sha of path+text head). Dedup by key, first wins. Pure.
    """
    entries: list[dict] = []
    seen: set[str] = set()
    for path, rec in rows:
        text = recoverable_text(rec)
        if not text:
            continue
        key = rec.get("input_sha256") or hashlib.sha256(
            f"{path}:{text[:64]}".encode()).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "key": key,
            "sha256": key,
            "text": text,
            "op": rec.get("op", "?"),
            "ts": rec.get("ts", ""),
        })
    return entries


def _cos(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        # A silent zip-truncation here would return confident wrong cosines —
        # the exact failure shape the capability probe exists to prevent.
        raise ValueError(f"cosine dim mismatch: {len(a)} vs {len(b)} — rebuild the index "
                         f"(semantic-index) or pass --model; dims must agree")
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb) ** 0.5


def dedup_pairs(vectors: list[list[float]], threshold: float = _DEDUP_THRESHOLD) -> list[tuple[int, int, float]]:
    """EVAL-ONLY near-dupe index pairs above the threshold, best first. Pure."""
    out: list[tuple[int, int, float]] = []
    for i in range(len(vectors)):
        vi = vectors[i]
        for j in range(i + 1, len(vectors)):
            score = _cos(vi, vectors[j])
            if score >= threshold:
                out.append((i, j, round(score, 4)))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def shortlist(query_vec: list[float], cand_vectors: list[list[float]], k: int) -> list[int]:
    """Pure top-k: candidate indexes nearest the query vector. Pure."""
    scored = [(_cos(query_vec, v), i) for i, v in enumerate(cand_vectors)]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [i for _s, i in scored[: max(0, k)]]


def load_feedback_by_sha(feedback_file: Path | None = None) -> dict[str, list[dict]]:
    """input_sha256 -> [{label, task, note, ts}] from the feedback store."""
    fb = Path(feedback_file) if feedback_file else FEEDBACK_FILE
    out: dict[str, list[dict]] = {}
    if not fb.exists():
        return out
    for line in fb.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        sha = rec.get("input_sha256")
        if sha:
            out.setdefault(sha, []).append({
                "label": rec.get("label"),
                "task": rec.get("task"),
                "note": (rec.get("note") or "")[:120],
                "ts": rec.get("ts"),
            })
    return out


# ── commands ────────────────────────────────────────────────────────────────


def _embed_items(items: list[dict], provider: str, endpoint: str | None = None):
    if sem1 is None:
        print("error: sem1 not importable — set DEV_DECISIONS_SEM1_PATH or install ~/Projects/sem1",
              file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    kwargs = {"provider": provider}
    if provider == "llama-server":
        kwargs["model"] = _MODEL
        if endpoint:
            kwargs["endpoint"] = endpoint
    try:
        return sem1.embed(items, **kwargs)
    except sem1.Sem1Error as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def _embed_batch(texts: list[str], provider: str, endpoint: str | None = None):
    return _embed_items([{"text": t} for t in texts], provider, endpoint)


def _resolve_store_and_model(args: argparse.Namespace):
    """(VectorStore, model) for a corpus-aware command. Default path is
    byte-identical to pre-corpus behavior (VECTORS_DIR, _MODEL); when that
    model store is absent but the root holds exactly one model store, resolve
    to it — named corpora are model-keyed too, and the caller should not have
    to know the slug. `--vectors-dir` overrides the root for fixtures/tests."""
    from sem1.store import VectorStore

    root = Path(args.vectors_dir) if getattr(args, "vectors_dir", None) else corpus_root(getattr(args, "corpus", None))
    vs = VectorStore(root)
    model = getattr(args, "model", None) or _MODEL
    if not (vs._dir(model) / "manifest.json").exists():
        found = sorted(d.name for d in root.iterdir() if (d / "manifest.json").exists()) if root.is_dir() else []
        if len(found) == 1:
            model = found[0]
        elif not found:
            print("error: no index — run `dev-decisions semantic-index` first", file=sys.stderr)
            raise SystemExit(EXIT_ERROR)
        else:
            print(f"error: ambiguous model stores under {root} ({', '.join(found)}) — pass --model",
                  file=sys.stderr)
            raise SystemExit(EXIT_ERROR)
    return vs, model


def _provider_tag(provider: str) -> str:
    return f"{SEM1_RAW_TAG}/{provider}"


def _index_inputs_mode(args: argparse.Namespace) -> int:
    """`--inputs <dir>`: embed files from a directory into a named corpus.
    Extension-routed (text → llama-server, images → st-worker), idempotent per
    content sha: existing entries are carried forward and merged, so a corpus
    accumulates across runs and a double run rebuilds the same manifest."""
    from sem1.store import VectorStore

    root = Path(args.inputs)
    if not root.is_dir():
        print(f"error: --inputs directory not found: {root}", file=sys.stderr)
        return EXIT_ERROR
    parts = collect_input_files(root)
    vs = VectorStore(corpus_root(args.corpus))
    summary = {"op": "semantic-index", "inputs_mode": True, "corpus": args.corpus or _CALIBRATION_CORPUS,
               "text_files": len(parts["text"]), "image_files": len(parts["image"]),
               "skipped_files": parts["skipped"]}
    if not parts["text"] and not parts["image"]:
        msg = "semantic-index: no embeddable files under the inputs directory"
        if args.json:
            print(json.dumps({**summary, "ok": True, "indexed": 0, "note": msg}))
        else:
            print(msg)
        return EXIT_OK
    indexed_total = 0
    for modality, files in (("text", parts["text"]), ("image", parts["image"])):
        if not files:
            continue
        provider = "llama-server" if modality == "text" else "st-worker"
        if modality == "text":
            items = [{"text": p.read_text(errors="replace")[:_LOG_TEXT_CHARS]} for p in files]
        else:
            items = [{"image": str(p)} for p in files]
        result = _embed_items(items, provider, args.endpoint if provider == "llama-server" else None)
        entries, vec_map = [], {}
        for p, v in zip(files, result.vectors):
            key = hashlib.sha256(p.read_bytes()).hexdigest()
            entries.append({"key": key, "sha256": key, "path": str(p)})
            vec_map[key] = v
        existing_manifest, existing_vectors = vs.load(result.model)
        for e, vec in zip((existing_manifest or {}).get("entries") or [], existing_vectors or []):
            vec_map.setdefault(e["key"], vec)
        merged = merge_entries(existing_manifest, entries)
        missing = [e["key"] for e in merged if e["key"] not in vec_map]
        if missing:
            print(f"error: {len(missing)} merged entry/ies lost their vector — refusing a lossy rebuild",
                  file=sys.stderr)
            return EXIT_ERROR
        manifest = vs.rebuild(result.model, result.dims, result.provider,
                              [(e["key"], e["sha256"], vec_map[e["key"]]) for e in merged],
                              meta=[{k: e[k] for k in e if k not in ("key", "sha256")} for e in merged])
        indexed_total = manifest["count"]
        summary.update({"model": result.model, "dims": result.dims, "provider": result.provider})
        if not args.json:
            print(f"  {modality}: {len(files)} file(s) via {provider} → {manifest['count']} vectors "
                  f"({result.dims} dims, model {result.model})")
        log_record({
            "op": "semantic-index",
            "provider": _provider_tag(provider),
            "model": result.model,
            "corpus": args.corpus or _CALIBRATION_CORPUS,
            "inputs_mode": True,
            "rows_indexed": manifest["count"],
            "dims": result.dims,
            "verdict": "eval-only",
        })
    summary["indexed"] = indexed_total
    if args.json:
        print(json.dumps({**summary, "ok": True}))
    else:
        print(f"semantic-index: corpus '{summary['corpus']}' now holds {indexed_total} vector(s); "
              f"{parts['skipped']} unembeddable file(s) skipped")
        print("  note: EVAL-ONLY lane — no join or verdict consumes this index yet")
    return EXIT_OK


def cmd_semantic_index(args: argparse.Namespace) -> int:
    """Rebuild the vector index over the calibration stores (eval-only)."""
    from sem1.store import VectorStore

    if getattr(args, "inputs", None):
        return _index_inputs_mode(args)

    provider = args.provider or "llama-server"
    rows = list(iter_log_rows(args.log_dir))
    entries = build_entries(rows)
    skipped = len(rows) - len(entries)
    if not entries:
        if args.json:
            print(json.dumps({"op": "semantic-index", "ok": True, "indexed": 0, "rows_total": len(rows),
                              "note": "no rows with recoverable text — nothing to index"}))
        else:
            print("semantic-index: no rows with recoverable text — nothing to index")
        return EXIT_OK
    if not args.json:
        print(f"semantic-index: {len(entries)}/{len(rows)} rows have recoverable text "
              f"({skipped} redacted-only rows skipped); embedding via {provider} ...")
    result = _embed_batch([e["text"] for e in entries], provider, args.endpoint)
    vs = VectorStore(corpus_root(getattr(args, "corpus", None)))
    manifest = vs.rebuild(result.model, result.dims, provider,
                          [(e["key"], e["sha256"], v) for e, v in zip(entries, result.vectors)],
                          meta=[{"op": e["op"], "ts": e["ts"]} for e in entries])
    log_record({
        "op": "semantic-index",
        "provider": _provider_tag(provider),
        "model": result.model,
        "corpus": getattr(args, "corpus", None) or _CALIBRATION_CORPUS,
        "rows_total": len(rows),
        "rows_indexed": len(entries),
        "rows_skipped": skipped,
        "dims": result.dims,
        "latency_ms": result.telemetry.get("latency_ms"),
        "verdict": "eval-only",
    })
    if args.json:
        print(json.dumps({"op": "semantic-index", "ok": True, "corpus": getattr(args, "corpus", None) or _CALIBRATION_CORPUS,
                          "indexed": manifest["count"], "rows_total": len(rows), "rows_skipped": skipped,
                          "dims": result.dims, "model": result.model, "provider": provider,
                          "verdict": "eval-only"}))
    else:
        print(f"  indexed {manifest['count']} vectors ({result.dims} dims, model {result.model}) "
              f"under {vs.root}")
        print("  note: EVAL-ONLY lane — no join or verdict consumes this index yet")
    return EXIT_OK


def cmd_semantic_dedup(args: argparse.Namespace) -> int:
    """EVAL-ONLY near-dupe report over an indexed corpus."""
    vs, model = _resolve_store_and_model(args)
    manifest, vectors = vs.load(model)
    if manifest is None:
        print("error: no index — run `dev-decisions semantic-index` first", file=sys.stderr)
        return EXIT_ERROR
    entries = manifest["entries"]
    try:
        pairs = dedup_pairs(vectors, _DEDUP_THRESHOLD)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    fb = load_feedback_by_sha(args.feedback)
    shown = pairs[: args.limit]
    if args.json:
        print(json.dumps({"op": "semantic-dedup", "ok": True, "corpus": getattr(args, "corpus", None) or _CALIBRATION_CORPUS,
                          "model": manifest["model"], "count_indexed": len(entries),
                          "pairs": len(pairs), "threshold": _DEDUP_THRESHOLD, "verdict": "eval-only"}))
        for i, j, score in shown:
            ei, ej = entries[i], entries[j]
            print(json.dumps({"pair": [ei["key"], ej["key"]], "score": score,
                              "ops": [ei.get("op", "?"), ej.get("op", "?")],
                              "gradedA": _graded_rows(ei["key"], fb),
                              "gradedB": _graded_rows(ej["key"], fb)}))
    else:
        print(f"semantic-dedup (EVAL-ONLY, threshold {_DEDUP_THRESHOLD}): "
              f"{len(pairs)} near-dupe pair(s) over {len(entries)} indexed rows")
        for i, j, score in shown:
            ei, ej = entries[i], entries[j]
            print(f"  [{score}] {ei['key']} ({ei.get('op', '?')}) <-> {ej['key']} ({ej.get('op', '?')})")
            print(f"      graded A: {_print_graded(ei['key'], fb)}")
            print(f"      graded B: {_print_graded(ej['key'], fb)}")
    log_record({
        "op": "semantic-dedup",
        "provider": _provider_tag("index"),
        "model": manifest["model"],
        "corpus": getattr(args, "corpus", None) or _CALIBRATION_CORPUS,
        "pairs": len(pairs),
        "threshold": _DEDUP_THRESHOLD,
        "verdict": "eval-only",
    })
    return EXIT_OK


def cmd_semantic_nn(args: argparse.Namespace) -> int:
    """EVAL-ONLY nearest graded neighbors for a query text or file."""
    qpath = Path(args.file) if args.file else None
    if qpath is not None and not qpath.exists():
        print(f"error: file not found: {qpath}", file=sys.stderr)
        return EXIT_ERROR
    if qpath is None and not args.text and not getattr(args, "query_vector", None):
        print("error: pass --text or --file", file=sys.stderr)
        return EXIT_ERROR

    vs, model = _resolve_store_and_model(args)
    manifest, vectors = vs.load(model)
    if manifest is None:
        print("error: no index — run `dev-decisions semantic-index` first", file=sys.stderr)
        return EXIT_ERROR

    qvec = None
    qmodel = qprovider = None
    if getattr(args, "query_vector", None):
        qvec = [float(x) for x in str(args.query_vector).split(",") if x.strip()]
    else:
        # Extension routing for query files: an image query rides st-worker —
        # the only provider that can prove image capability — whatever
        # --provider says; text queries keep the provider flag's default.
        provider = args.provider or "llama-server"
        if qpath is not None and qpath.suffix.lower() in _IMAGE_EXTS:
            provider = "st-worker"
            items = [{"image": str(qpath)}]
        elif qpath is not None:
            items = [{"text": qpath.read_text(errors="replace")[:_LOG_TEXT_CHARS]}]
        else:
            items = [{"text": args.text}]
        result = _embed_items(items, provider, args.endpoint)
        qvec, qmodel, qprovider = result.vectors[0], result.model, provider

    if qmodel is None:
        qmodel, qprovider = manifest["model"], manifest.get("provider", "index")

    try:
        hits = shortlist(qvec, vectors, args.k)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    fb = load_feedback_by_sha(args.feedback)
    entries = manifest["entries"]
    scored = [(round(_cos(qvec, vectors[i]), 4), i) for i in hits]
    if args.json:
        print(json.dumps({"op": "semantic-nn", "ok": True, "corpus": getattr(args, "corpus", None) or _CALIBRATION_CORPUS,
                          "model": manifest["model"], "k": args.k, "hits": len(hits), "verdict": "eval-only"}))
        for score, idx in scored:
            e = entries[idx]
            print(json.dumps({"key": e["key"], "score": score, "op": e.get("op", "?"),
                              "ts": e.get("ts", ""), "path": e.get("path", ""),
                              "graded": _graded_rows(e["key"], fb)}))
    else:
        print(f"semantic-nn (EVAL-ONLY): {len(hits)} nearest graded neighbor(s) of "
              f"{args.file or repr(args.text[:60])}")
        for score, idx in scored:
            e = entries[idx]
            print(f"  [{score}] {e['key']} ({e.get('op', '?')}, {str(e.get('ts', ''))[:10]})")
            print(f"      graded: {_print_graded(e['key'], fb)}")
    log_record({
        "op": "semantic-nn",
        "provider": _provider_tag(qprovider),
        "model": manifest["model"],
        "corpus": getattr(args, "corpus", None) or _CALIBRATION_CORPUS,
        "k": args.k,
        "hits": [{"key": entries[i]["key"], "score": s} for s, i in scored],
        "verdict": "eval-only",
    })
    return EXIT_OK


def _graded_rows(sha: str, fb: dict[str, list[dict]]) -> list[dict]:
    return fb.get(sha, [])[:3]


def _print_graded(sha: str, fb: dict[str, list[dict]]) -> str:
    rows = _graded_rows(sha, fb)
    if not rows:
        return "ungraded"
    out = []
    for r in rows:
        s = f"{r.get('label')}@{r.get('task')}"
        if r.get("note"):
            s += f" ({r['note']})"
        out.append(s)
    return "; ".join(out)
