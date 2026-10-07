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


def _embed_batch(texts: list[str], provider: str, endpoint: str | None = None):
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
        return sem1.embed([{"text": t} for t in texts], **kwargs)
    except sem1.Sem1Error as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def _provider_tag(provider: str) -> str:
    return f"{SEM1_RAW_TAG}/{provider}"


def cmd_semantic_index(args: argparse.Namespace) -> int:
    """Rebuild the vector index over the calibration stores (eval-only)."""
    from sem1.store import VectorStore

    provider = args.provider or "llama-server"
    rows = list(iter_log_rows(args.log_dir))
    entries = build_entries(rows)
    skipped = len(rows) - len(entries)
    if not entries:
        print("semantic-index: no rows with recoverable text — nothing to index")
        return EXIT_OK
    print(f"semantic-index: {len(entries)}/{len(rows)} rows have recoverable text "
          f"({skipped} redacted-only rows skipped); embedding via {provider} ...")
    result = _embed_batch([e["text"] for e in entries], provider, args.endpoint)
    vs = VectorStore(VECTORS_DIR)
    manifest = vs.rebuild(result.model, result.dims, provider,
                          [(e["key"], e["sha256"], v) for e, v in zip(entries, result.vectors)],
                          meta=[{"op": e["op"], "ts": e["ts"]} for e in entries])
    log_record({
        "op": "semantic-index",
        "provider": _provider_tag(provider),
        "model": result.model,
        "rows_total": len(rows),
        "rows_indexed": len(entries),
        "rows_skipped": skipped,
        "dims": result.dims,
        "latency_ms": result.telemetry.get("latency_ms"),
        "verdict": "eval-only",
    })
    print(f"  indexed {manifest['count']} vectors ({result.dims} dims, model {result.model}) "
          f"under {VECTORS_DIR}")
    print("  note: EVAL-ONLY lane — no join or verdict consumes this index yet")
    return EXIT_OK


def cmd_semantic_dedup(args: argparse.Namespace) -> int:
    """EVAL-ONLY near-dupe report over the indexed calibration store."""
    from sem1.store import VectorStore

    vs = VectorStore(VECTORS_DIR)
    manifest, vectors = vs.load(args.model or _MODEL)
    if manifest is None:
        print("error: no index — run `dev-decisions semantic-index` first", file=sys.stderr)
        return EXIT_ERROR
    entries = manifest["entries"]
    pairs = dedup_pairs(vectors, _DEDUP_THRESHOLD)
    fb = load_feedback_by_sha(args.feedback)
    print(f"semantic-dedup (EVAL-ONLY, threshold {_DEDUP_THRESHOLD}): "
          f"{len(pairs)} near-dupe pair(s) over {len(entries)} indexed rows")
    for i, j, score in pairs[: args.limit]:
        ei, ej = entries[i], entries[j]
        print(f"  [{score}] {ei['key']} ({ei.get('op', '?')}) <-> {ej['key']} ({ej.get('op', '?')})")
        print(f"      graded A: {_print_graded(ei['key'], fb)}")
        print(f"      graded B: {_print_graded(ej['key'], fb)}")
    log_record({
        "op": "semantic-dedup",
        "provider": _provider_tag("index"),
        "model": manifest["model"],
        "pairs": len(pairs),
        "threshold": _DEDUP_THRESHOLD,
        "verdict": "eval-only",
    })
    return EXIT_OK


def cmd_semantic_nn(args: argparse.Namespace) -> int:
    """EVAL-ONLY nearest graded neighbors for a query text or file."""
    from sem1.store import VectorStore

    query = None
    if args.file:
        p = Path(args.file)
        if not p.exists():
            print(f"error: file not found: {p}", file=sys.stderr)
            return EXIT_ERROR
        query = p.read_text()[:_LOG_TEXT_CHARS]
    elif args.text:
        query = args.text
    else:
        print("error: pass --text or --file", file=sys.stderr)
        return EXIT_ERROR

    vs = VectorStore(VECTORS_DIR)
    manifest, vectors = vs.load(args.model or _MODEL)
    if manifest is None:
        print("error: no index — run `dev-decisions semantic-index` first", file=sys.stderr)
        return EXIT_ERROR
    provider = args.provider or "llama-server"
    result = _embed_batch([query], provider, args.endpoint)
    hits = shortlist(result.vectors[0], vectors, args.k)
    fb = load_feedback_by_sha(args.feedback)
    entries = manifest["entries"]
    print(f"semantic-nn (EVAL-ONLY): {len(hits)} nearest graded neighbor(s) of "
          f"{args.file or repr(args.text[:60])}")
    for idx in hits:
        e = entries[idx]
        print(f"  [{round(_cos(result.vectors[0], vectors[idx]), 4)}] {e['key']} ({e.get('op', '?')}, {str(e.get('ts', ''))[:10]})")
        print(f"      graded: {_print_graded(e['key'], fb)}")
    log_record({
        "op": "semantic-nn",
        "provider": _provider_tag(provider),
        "model": result.model,
        "k": args.k,
        "hits": [{"key": entries[i]["key"], "score": round(_cos(result.vectors[0], vectors[i]), 4)}
                 for i in hits],
        "verdict": "eval-only",
    })
    return EXIT_OK


def _print_graded(sha: str, fb: dict[str, list[dict]]) -> str:
    rows = fb.get(sha, [])
    if not rows:
        return "ungraded"
    out = []
    for r in rows[:3]:
        s = f"{r.get('label')}@{r.get('task')}"
        if r.get("note"):
            s += f" ({r['note']})"
        out.append(s)
    return "; ".join(out)
