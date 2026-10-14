"""Synthesis lane: blend1 explains the gate verdicts.

The fifth model class under the same accountability machinery. sys1 judges
what the work SAYS, sdm1/tabular scores what the work MEASURES, sem1 indexes
what the work LOOKS LIKE, gen1 renders what the work needs HEARD AND SEEN,
and this lane COMPOSES: every verdict-emitting gate gains a second beat — a
synthesized, validated explanation of what the verdict means and what the
author should change, riding in the JSONL beside the verdict and keyed by
its input_sha256. The calibration dataset upgrades from scores to readable
causes.

Operating rule (2026-10-14 plan): BLEND1 PROPOSES PROSE, THE GATE DISPOSES.
Everything here is eval-only (tag `blend1_raw` in providers_used) — an
explanation is not calibrated and joins nothing; the gate's verdict stands
exactly as recorded, and nothing in this module gates, blocks, or applies a
verdict. The only op this lane ever writes is `explain`.

Batch-only rule: like every other lane, no command here may run inside a
synchronous hook path; hooks never reference this module (socket-guard
test). `explain` spends synthesis tokens per ask (~$0.0015 synthesized,
~$0 direct-path) — batch it, never hook it; `--dry-run` rehearses spend-free.

Artifact integrity: `explain` takes --artifact PATH, the same input the gate
judged (a diff, a plan file). The artifact is sha256-hashed and a mismatch
with the row's input_sha256 is a named refusal — explaining a drifted
artifact is worse than not explaining.

Recoverable text: the JSONL stores are redacted by design, so the
explanation text never enters the log; it lands on disk at
LOG_DIR/feedback/explanations/<gate input_sha256>.md and the row carries
explanation_sha256, explanation_chars, the ask's route/gate telemetry, and
the linked input_sha256 — the same recoverable-text pattern the semantic
lane indexes.

blend1 is consumed contract-only (one `ask(prompt)` on the default auto
path — the router decides synthesis; live=True is never passed) and imported
lazily: a missing install fails open naming the install, like every other
optional sibling. blend1's own telemetry sink records every ask, failed ones
included; this module only reads what it needs for its own rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .config import EXIT_ERROR, EXIT_OK, LOG_DIR, log_record

# section: synthesis

BLEND1_RAW_TAG = "blend1_raw"        # providers_used tag until calibration floors exist
EXPLANATIONS_DIR = LOG_DIR / "feedback" / "explanations"
_INSTALL_HINT = ("blend1 lane is not installed: pip install -e ~/Projects/blend1 "
                 "(the synthesis lane's sibling)")
# The verdict-emitting ops in the event log (the rows `explain` may target).
GATE_OPS = {
    "plan-gate", "arch-gate", "pr-gate", "evidence-gate", "docs-gate",
    "zcode-gate", "classify-diff", "scan-staged", "media-gate", "uc-gate", "ux-gate",
}
# Verdicts that are not failed/inconclusive; anything else counts as failed
# for --last-failed (gaps, drift, not-supported, escalated, warn, block, ...).
PASS_VERDICTS = {"pass", "clean", "supported", "ok"}
_LOG_TEXT_CHARS = 8_000              # artifact-text cap (semantics.py's bound)
_ANSWERS_CHARS = 4_000               # gate-heads cap inside the ask prompt


# ── pure layer (fixture-tested; no I/O beyond what callers pass in) ─────────


def _sha_matches(stored: str, wanted: str) -> bool:
    """The log stores 16-char prefixes (the house _sha16 convention); a
    caller may pass the prefix or a full digest, so either direction of
    prefix wins. Nothing fuzzy — a shared prefix is a match, nothing else."""
    if not stored or not wanted:
        return False
    return stored == wanted or stored.startswith(wanted) or wanted.startswith(stored)


def _row_providers(row: dict) -> list[str]:
    """The gate row's provider tags as a list — providers_used when present,
    else the single provider field. Empty when the row names none."""
    used = row.get("providers_used")
    if isinstance(used, list) and used:
        return [str(p) for p in used]
    provider = row.get("provider")
    return [str(provider)] if provider else []


def _explain_prompt(row: dict, artifact_text: str) -> str:
    """The ask blend1 answers: what this gate verdict means and what the
    author should change, using only the artifact's content. Every field
    carried in is already log-safe (the stores are redacted by design); the
    artifact text is the same input the gate judged. Pure."""
    parts = [
        "A dev-decisions gate judged the artifact below and returned the "
        "verdict that follows. Explain, in prose a developer can act on, "
        "what this verdict means and what the author should change. Use only "
        "the artifact's content — never invent facts the gate did not record.",
        f"GATE OP: {row.get('op')}",
        f"VERDICT: {row.get('verdict')}",
    ]
    answers = row.get("answers") or row.get("heads")
    if answers:
        parts.append("GATE HEADS (nouls and labels as recorded):\n"
                     + json.dumps(answers, sort_keys=True, default=str)[:_ANSWERS_CHARS])
    providers = _row_providers(row)
    if providers:
        parts.append("PROVIDERS: " + ", ".join(providers))
    parts.append("ARTIFACT THE GATE JUDGED:\n" + artifact_text[:_LOG_TEXT_CHARS])
    return "\n\n".join(parts)


def _sub(d: dict, key: str) -> dict:
    """A dict-valued sub-report (route/generated/validation), or {} when the
    ask result carries none. Pure."""
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def _as_result_dict(result: object) -> dict:
    """blend1.ask returns a BlendResult (as_dict()); a plain dict is accepted
    too (the test stub's shape). One shape either way. Pure."""
    if isinstance(result, dict):
        return result
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        try:
            out = as_dict()
        except Exception:
            return {}
        return dict(out) if isinstance(out, dict) else {}
    return {}


# ── the lane ask ────────────────────────────────────────────────────────────


def _blend1():
    """Lazy sibling import. None when blend1 is not installed — every caller
    fails open naming the install."""
    try:
        import blend1  # type: ignore[import-not-found]
        return blend1
    except ImportError:
        return None


def explain_gate_row(row: dict, artifact_path: str) -> dict:
    """One gate row + the artifact it judged → one validated explanation.

    Never raises. On success returns the redacted explain row (the
    explanation text never rides in it — it lands at
    EXPLANATIONS_DIR/<input_sha256>.md) and the row's shape is exactly:
    op/input_sha256/explained_op/provider/providers_used/route/validation/
    explanation_sha256/explanation_chars/cost_usd/latency_ms. On any refusal
    returns {"status": "error", "error": <named reason>} — the hash gate, the
    missing install, an unreadable artifact, or an ask that produced nothing.
    """
    try:
        return _explain_gate_row(row, artifact_path)
    except Exception as e:  # the covenant: a lane wrapper never raises
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _explain_gate_row(row: dict, artifact_path: str) -> dict:
    row = row if isinstance(row, dict) else {}
    sha = str(row.get("input_sha256") or "")
    if not sha:
        return {"status": "error",
                "error": "gate row carries no input_sha256 — nothing to key the explanation to"}
    blend1 = _blend1()
    if blend1 is None:
        return {"status": "error", "error": _INSTALL_HINT}
    path = Path(artifact_path)
    try:
        data = path.read_bytes()
    except OSError as e:
        return {"status": "error", "error": f"artifact {path} unreadable: {e}"}
    # Artifact integrity: the row's input_sha256 is the hash of what the gate
    # judged (16-char prefix, the house convention). A drifted artifact is
    # refused by name — explaining it would attach prose to the wrong input.
    actual = hashlib.sha256(data).hexdigest()
    if not _sha_matches(sha, actual):
        return {"status": "error",
                "error": (f"artifact {path} hashes to {actual[:16]}, the gate row "
                          f"hashes to {sha} — refusing to explain a drifted artifact")}
    prompt = _explain_prompt(row, data.decode("utf-8", errors="replace"))
    try:
        # The default (auto) path: the router decides synthesis. live=True is
        # never passed — gate explanations need no live facts.
        result = blend1.ask(prompt)
    except Exception as e:
        return {"status": "error", "error": f"blend1 ask failed: {type(e).__name__}: {e}"}
    d = _as_result_dict(result)
    generated = _sub(d, "generated")
    text = str(d.get("final_answer") or generated.get("text") or "")
    if not text.strip():
        reason = generated.get("error") or d.get("error") or "blend1 returned no explanation text"
        return {"status": "error", "error": f"blend1 did not explain: {reason}"}
    route = _sub(d, "route")
    validation = _sub(d, "validation")
    out = EXPLANATIONS_DIR / f"{sha}.md"
    try:
        EXPLANATIONS_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError as e:
        return {"status": "error", "error": f"cannot write {out}: {e}"}
    return {
        "op": "explain",
        "input_sha256": sha,
        "explained_op": row.get("op"),
        "provider": "blend1",
        "providers_used": _row_providers(row) + [BLEND1_RAW_TAG],
        "route": {
            "needs_generation": route.get("needs_generation"),
            "needs_live_facts": route.get("needs_live_facts"),
            "answer_shape": route.get("answer_shape"),
        },
        "validation": {
            "outcome": validation.get("outcome"),
            "answers_the_question": validation.get("answers_the_question"),
            "contradicts_facts": validation.get("contradicts_facts"),
            "number_check": validation.get("number_check"),
        },
        "explanation_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "explanation_chars": len(text),
        "cost_usd": generated.get("cost_usd"),
        "latency_ms": d.get("total_latency_ms"),
    }


# ── log readers (mirrors semantics.iter_log_rows: same glob, feedback
# stores excluded — explanation files and feedback rows are not gate events)


def _iter_event_rows(log_dir: Path | None = None):
    """Yield (path, record) for every parseable JSONL row in the event logs."""
    root = Path(log_dir) if log_dir else LOG_DIR
    for path in sorted(root.rglob("*.jsonl")):
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


def _select_gate_rows(rows, *, row: str | None = None, last_failed: bool = False,
                      date: str | None = None) -> tuple[list, str | None]:
    """Pick the gate row(s) a selector names. rows: (path, record) pairs in
    log order (chronological). Returns (selected, None) or ([], reason) —
    every miss is named, never an empty silent success. Pure."""
    gate_rows = [(p, r) for p, r in rows
                 if r.get("op") in GATE_OPS and r.get("input_sha256")]
    if row:
        matched = [(p, r) for p, r in gate_rows
                   if _sha_matches(str(r.get("input_sha256")), row)]
        if not matched:
            return [], (f"no gate row matches --row {row} "
                        f"(gate ops: {', '.join(sorted(GATE_OPS))})")
        return [matched[-1]], None      # the freshest verdict for that artifact
    if last_failed:
        failed = [(p, r) for p, r in gate_rows
                  if str(r.get("verdict") or "") not in PASS_VERDICTS]
        if not failed:
            return [], "no gate row with a failed/inconclusive verdict in the log"
        return [failed[-1]], None
    if date:
        dated = [(p, r) for p, r in gate_rows
                 if str(r.get("ts") or "").startswith(date)]
        if not dated:
            return [], f"no gate rows logged on {date} (UTC)"
        latest: dict[str, tuple[Path, dict]] = {}
        for pair in dated:              # one explanation per distinct artifact —
            latest[str(pair[1]["input_sha256"])] = pair   # freshest row wins
        return list(latest.values()), None
    return [], "one of --row, --last-failed, --date is required"


def synthesis_budget() -> dict:
    """Aggregate the blend1_raw rows: ask count, total spend (None-safe —
    direct-path asks cost nothing and carry no cost field), per-op
    breakdown, first/last ts. Reads the same event-log glob the semantic
    lane reads. Pure over the log; never raises on a corrupt line."""
    asks = 0
    cost_total = 0.0
    cost_rows = 0
    by_op: dict[str, dict] = {}
    first_ts = last_ts = None
    for _path, rec in _iter_event_rows():
        providers = rec.get("providers_used")
        if not (isinstance(providers, list) and BLEND1_RAW_TAG in providers):
            continue
        asks += 1
        op = str(rec.get("op") or "explain")
        bucket = by_op.setdefault(op, {"asks": 0, "cost_usd": 0.0})
        bucket["asks"] += 1
        cost = rec.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost_total += float(cost)
            cost_rows += 1
            bucket["cost_usd"] = round(bucket["cost_usd"] + float(cost), 6)
        ts = rec.get("ts")
        if isinstance(ts, str) and ts:
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts
    return {
        "op": "synthesis-budget",
        "ok": True,
        "tag": BLEND1_RAW_TAG,
        "asks": asks,
        "cost_usd_total": round(cost_total, 6),
        "rows_with_cost": cost_rows,
        "rows_missing_cost": asks - cost_rows,
        "by_op": by_op,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "verdict": "ok" if asks else "no-data",
    }


# ── commands ────────────────────────────────────────────────────────────────


def cmd_explain(args: argparse.Namespace) -> int:
    """explain: resolve a gate row (--row SHA | --last-failed | --date),
    hash the --artifact against the row's input_sha256, ask blend1 for a
    validated explanation, write it to EXPLANATIONS_DIR/<sha>.md, log the
    redacted row, and print the explanation plus its path. --dry-run
    resolves and hashes only: no ask, no row, no file. Batch-only (spends
    synthesis tokens per ask). Every refusal is named on stderr; no
    traceback ever reaches the caller."""
    artifact = Path(args.artifact)
    if not artifact.is_file():
        print(f"error: --artifact not found: {artifact}", file=sys.stderr)
        return EXIT_ERROR
    rows = list(_iter_event_rows())
    selected, err = _select_gate_rows(rows, row=getattr(args, "row", None),
                                      last_failed=getattr(args, "last_failed", False),
                                      date=getattr(args, "date", None))
    if err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_ERROR

    if getattr(args, "dry_run", False):
        ok = True
        try:
            actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError as e:
            print(f"error: artifact {artifact} unreadable: {e}", file=sys.stderr)
            return EXIT_ERROR
        for _path, rec in selected:
            sha = str(rec.get("input_sha256"))
            if _sha_matches(sha, actual):
                print(f"explain: dry run — resolved {rec.get('op')} row {sha} "
                      f"(verdict {rec.get('verdict')}, ts {rec.get('ts')})")
                print(f"  artifact {artifact} hashes to {actual[:16]} — matches the row")
                print("  no ask, no row, no file (--dry-run)")
            else:
                print(f"error: artifact {artifact} hashes to {actual[:16]}, the gate "
                      f"row hashes to {sha} — refusing to explain a drifted artifact",
                      file=sys.stderr)
                ok = False
        return EXIT_OK if ok else EXIT_ERROR

    explained = refused = 0
    for _path, rec in selected:
        out_row = explain_gate_row(rec, str(artifact))
        if out_row.get("op") != "explain":
            print(f"explain: refused — {out_row.get('error')}", file=sys.stderr)
            refused += 1
            continue
        log_record(out_row)
        explained += 1
        validation = out_row.get("validation") or {}
        path = EXPLANATIONS_DIR / f"{out_row['input_sha256']}.md"
        print(f"explain: {out_row['explained_op']} row {out_row['input_sha256']} "
              f"explained via blend1 — validation {validation.get('outcome')}, "
              f"{out_row['explanation_chars']} chars, "
              f"cost_usd {out_row.get('cost_usd')}, latency_ms {out_row.get('latency_ms')}")
        print(f"  explanation: {path}")
        try:
            print(path.read_text(encoding="utf-8"))
        except OSError as e:
            print(f"  (explanation file unreadable: {e})", file=sys.stderr)
    if refused and not explained:
        return EXIT_ERROR
    return EXIT_OK


def cmd_synthesis_budget(args: argparse.Namespace) -> int:
    """synthesis-budget: the blend1 lane's spend visibility — ask counts and
    estimated cost from the blend1_raw rows, per-op breakdown, first/last ts.
    Prints the report as JSON. Eval-only spend stays visible; the read never
    writes a row."""
    print(json.dumps(synthesis_budget()))
    return EXIT_OK
