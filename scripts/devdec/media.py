"""Media generation lane: gen1 batch commands (speak / transcribe / imagine).

The fourth model class under the same accountability machinery. sys1 judges
what the work SAYS, sdm1/tabular scores what the work MEASURES, sem1 indexes
what the work LOOKS LIKE, and this lane generates what the work needs HEARD
AND SEEN: narration round-trips verified against their scripts, ASR accuracy
recorded against pinned ground truth, media usage recorded and forecast.

Operating rules (2026-10-08 plan):

- Batch-only (the tabular lesson, re-stated): speak, transcribe, and imagine
  are each network or subprocess, so no command here may run inside a
  synchronous hook path. Hooks never reference this module (socket-guard and
  hook-source tests in TestGen1Lane).
- Eval-only until calibrated (tag ``gen1_raw/<leg>`` in providers_used): the
  gate's verdict rides an uncalibrated ASR, so media-gate is advisory — WARN
  on gaps, never BLOCK — until record-asr accumulates the graded rows that
  earn floors. Rendering itself is never judged: media renders what the work
  needs; whether it's good stays with the owner and the gates.
- The fail-open wrapper covenant (tabular_classify's shape): wrappers never
  raise, return {ok, …, error}, and every refusal is named — the missing
  variable and the file gen1 looked in, or the provider's own Gen1Error.
- Redacted rows: log rows carry agreement, token counts, and sha256s —
  never transcript content, never key material. gen1's telemetry sink is
  structurally redacted on its side; this module only reads it.

gen1 is consumed contract-only (public surface speak/transcribe/imagine,
probe/doctor, registry_names, the error taxonomy); this repo changes nothing
next door. Result keys are additive-only by house rule, so the wrappers read
only the pinned keys.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import EXIT_ERROR, EXIT_OK, EXIT_WARN, LOG_DIR, log_record
from .judgment import gen1
from .tabular import TABLES_DIR, forecast_band, read_table, write_table
from .workflow import _sha16

# section: media

GEN1_RAW_TAG = "gen1_raw"           # providers_used tag until calibration floors exist
FEEDBACK_FILE = LOG_DIR / "feedback" / "feedback.jsonl"
MEDIA_RUNS_TABLE = "media_runs.csv"
# the in-repo round-trip fixtures (sha256s pinned in the selftest)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "media"


# ── C1: readiness probe ──────────────────────────────────────────────────────


def gen1_ready() -> tuple[bool, str]:
    """(ready, reason) — library importable and at least one provider registered."""
    if gen1 is None:
        return False, "gen1 not importable — set DEV_DECISIONS_GEN1_PATH or install ~/Projects/gen1"
    try:
        names = gen1.registry_names()
        if names:
            return True, f"gen1 + {', '.join(names)} available"
        return False, "gen1 registry empty — update ~/Projects/gen1"
    except Exception as e:
        return False, f"gen1 registry unreadable: {e}"


# ── C2: fail-open wrappers (the tabular_classify covenant) + comparator ─────


def media_transcribe(audio: bytes, *, language: str | None = None,
                     model: str | None = None, provider: str | None = None) -> dict:
    """Audio → text. Never raises: {ok, transcript, result, error}."""
    if gen1 is None:
        return {"ok": False, "transcript": None, "result": None,
                "error": "gen1 not importable — set DEV_DECISIONS_GEN1_PATH or install ~/Projects/gen1"}
    try:
        result = gen1.transcribe(audio, language=language, model=model, provider=provider)
    except Exception as e:
        return {"ok": False, "transcript": None, "result": None, "error": str(e)}
    return {"ok": True, "transcript": result.text, "result": result, "error": None}


def media_speak(text: str, *, voice: str | None = None, language: str = "en",
                format: str | None = None, provider: str | None = None,
                speed: float | None = None, instruction: str | None = None) -> dict:
    """Text → speech. Never raises: {ok, audio, result, error}."""
    if gen1 is None:
        return {"ok": False, "audio": None, "result": None,
                "error": "gen1 not importable — set DEV_DECISIONS_GEN1_PATH or install ~/Projects/gen1"}
    try:
        result = gen1.speak(text, voice=voice, language=language, format=format,
                            provider=provider, speed=speed, instruction=instruction)
    except Exception as e:
        return {"ok": False, "audio": None, "result": None, "error": str(e)}
    return {"ok": True, "audio": result.audio, "result": result, "error": None}


def media_imagine(prompt: str, *, aspect_ratio: str = "1:1", n: int = 1,
                  provider: str | None = None) -> dict:
    """Prompt → image(s). Never raises: {ok, images, result, error}."""
    if gen1 is None:
        return {"ok": False, "images": None, "result": None,
                "error": "gen1 not importable — set DEV_DECISIONS_GEN1_PATH or install ~/Projects/gen1"}
    try:
        result = gen1.imagine(prompt, aspect_ratio=aspect_ratio, n=n, provider=provider)
    except Exception as e:
        return {"ok": False, "images": None, "result": None, "error": str(e)}
    return {"ok": True, "images": result.images, "result": result, "error": None}


def leg_tag(result) -> str:
    """providers_used tag for a successful leg: gen1_raw/<provider>."""
    leg = (getattr(result, "provider", "") or "").strip() or "unknown"
    return f"{GEN1_RAW_TAG}/{leg}"


def compare_text(reference: str, transcript: str) -> dict:
    """Deterministic token agreement (no model, no head): lowercased
    alphanumeric tokens, order-preserving difflib match. Punctuation never
    counts — StepFun ASR normalizes it (the em-dash does not come back), so
    every comparison is token-level, never byte-exact (gen1's own C2 rule).

    Returns {agreement, matched, ref_tokens, got_tokens, missing, extra,
    missing_tokens, extra_tokens}. agreement = matched / max(len(ref), len(got))
    (1.0 only when the token sequences agree exactly; both-empty = 1.0).
    """
    ref = [t for t in re.findall(r"[^\W_]+", reference.lower())]
    got = [t for t in re.findall(r"[^\W_]+", transcript.lower())]
    sm = difflib.SequenceMatcher(None, ref, got, autojunk=False)
    matched = sum(m.size for m in sm.get_matching_blocks())
    missing_tokens: list[str] = []
    extra_tokens: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            missing_tokens.extend(ref[i1:i2])
        if tag in ("insert", "replace"):
            extra_tokens.extend(got[j1:j2])
    if ref or got:
        agreement = matched / max(len(ref), len(got))
    else:
        agreement = 1.0
    return {
        "agreement": round(agreement, 4),
        "matched": matched,
        "ref_tokens": len(ref),
        "got_tokens": len(got),
        "missing": len(ref) - matched,
        "extra": len(got) - matched,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
    }


# ── C3: media-gate — verify a rendered narration against its script ──────────


def _resolve_under_project(project: Path, rel: str) -> tuple[Path | None, str]:
    """Resolve a project-relative audio path. A path that escapes the project
    dir (traversal, absolute, symlink out) is refused by name — rendered audio
    lives inside the render project, and the gate must not read elsewhere."""
    try:
        base = project.resolve()
        candidate = Path(rel)
        target = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    except OSError as e:
        return None, f"unresolvable path {rel}: {e}"
    if target != base and not target.is_relative_to(base):
        return None, f"path escapes --project: {rel}"
    return target, ""


def _load_seam(request_path: Path, meta_path: Path, project: Path) -> tuple[list[dict], list[str]]:
    """Seam mode: request lines joined to their rendered audio files (meta
    voices), every refusal named. Returns (units, notes) where each unit is
    {id, text, audio, audio_sha, error} and notes flag unpaired render files."""
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return [{"id": "?", "text": "", "audio": None, "audio_sha": None,
                 "error": f"unreadable seam: {e}"}], []
    voices: dict[str, dict] = {}
    for v in meta.get("voices", []) or []:
        if isinstance(v, dict) and v.get("id") is not None:
            voices[str(v["id"])] = v
    units: list[dict] = []
    seen_ids: set[str] = set()
    for line in request.get("lines", []) or []:
        if not isinstance(line, dict):
            continue
        lid = str(line.get("id", f"line_{len(units) + 1}"))
        seen_ids.add(lid)
        text = str(line.get("text", ""))
        unit = {"id": lid, "text": text, "audio": None, "audio_sha": None, "error": None}
        voice = voices.get(lid)
        if voice is None:
            unit["error"] = f"no rendered audio for line {lid}"
            units.append(unit)
            continue
        target, err = _resolve_under_project(project, str(voice.get("path", "")))
        if target is None:
            unit["error"] = err
            units.append(unit)
            continue
        if not target.is_file():
            unit["error"] = f"audio missing: {voice.get('path')}"
            units.append(unit)
            continue
        try:
            unit["audio"] = target.read_bytes()
            unit["audio_sha"] = hashlib.sha256(unit["audio"]).hexdigest()
        except OSError as e:
            unit["error"] = f"audio unreadable: {e}"
        units.append(unit)
    unpaired = sorted(set(voices) - seen_ids)
    notes = [f"rendered file for {', '.join(unpaired)} has no request line"] if unpaired else []
    return units, notes


def cmd_media_gate(args: argparse.Namespace) -> int:
    """C3: verify a rendered narration against its script — the seam's
    request + meta (paths resolved against --project) or a single
    --script/--audio pair. Prints per-line agreement plus missing/extra tokens
    and writes one redacted log row (counts and sha256s, never content).

    Advisory by design: WARN on any gap, EXIT_ERROR when nothing could be
    verified, and it can never block — the verdict rides an uncalibrated ASR
    until record-asr accumulates the graded rows that earn floors."""
    if args.request:
        if not args.meta or not args.project or args.script or args.audio:
            print("usage: media-gate --request R --meta M --project P   (seam mode)")
            return EXIT_ERROR
    elif not (args.script and args.audio):
        print("usage: media-gate --script S --audio A   (single-file mode)")
        print("   or: media-gate --request R --meta M --project P   (seam mode)")
        return EXIT_ERROR

    notes: list[str] = []
    if args.request:
        units, notes = _load_seam(Path(args.request), Path(args.meta), Path(args.project))
        mode = "seam"
        target = str(args.request)
    else:
        script_path = Path(args.script)
        try:
            script_text = script_path.read_text(encoding="utf-8")
            audio = Path(args.audio).read_bytes()
        except OSError as e:
            print(f"media-gate: cannot read script/audio: {e}")
            return EXIT_ERROR
        units = [{"id": script_path.stem or "script", "text": script_text, "audio": audio,
                  "audio_sha": hashlib.sha256(audio).hexdigest(), "error": None}]
        mode = "single"
        target = str(args.script)

    if not units:
        print("media-gate: nothing to verify (empty request)")
        return EXIT_ERROR

    language = getattr(args, "language", None)
    verified: list[dict] = []
    refused: list[dict] = []
    legs: set[str] = set()
    for u in units:
        if u["error"] is not None:
            refused.append({"id": u["id"], "error": u["error"]})
            continue
        res = media_transcribe(u["audio"], language=language)
        if not res.get("ok"):
            refused.append({"id": u["id"], "error": res.get("error") or "transcription refused"})
            continue
        legs.add(leg_tag(res.get("result")))
        cmp_ = compare_text(u["text"], res.get("transcript") or "")
        verified.append({"id": u["id"], "agreement": cmp_["agreement"],
                         "ref_tokens": cmp_["ref_tokens"], "got_tokens": cmp_["got_tokens"],
                         "missing": cmp_["missing"], "extra": cmp_["extra"],
                         "audio_sha256": u["audio_sha"],
                         "_missing_tokens": cmp_["missing_tokens"],
                         "_extra_tokens": cmp_["extra_tokens"]})

    print(f"media-gate: {target} ({mode} mode, {'+'.join(sorted(legs)) or GEN1_RAW_TAG})")
    by_id = {v["id"]: v for v in verified}
    for u in units:
        v = by_id.get(u["id"])
        if v is None:
            continue
        gap = "ok" if (v["missing"] == 0 and v["extra"] == 0) else "gaps"
        print(f"  {u['id']}: agreement={v['agreement']:.3f} "
              f"({v['ref_tokens']} script / {v['got_tokens']} transcript tokens) {gap}")
        if v["_missing_tokens"]:
            print(f"    missing: {' '.join(v['_missing_tokens'])}")
        if v["_extra_tokens"]:
            print(f"    extra:   {' '.join(v['_extra_tokens'])}")
    for r in refused:
        print(f"  {r['id']}: refused — {r['error']}")
    for note in notes:
        print(f"  note: {note}")

    gap_lines = sum(1 for v in verified if v["missing"] or v["extra"])
    if not verified:
        verdict = "error"
        print(f"media-gate: all {len(units)} line(s) refused — nothing verified")
        rc = EXIT_ERROR
    elif gap_lines or refused:
        verdict = "gaps"
        print(f"media-gate: verdict gaps ({gap_lines} line(s) differ, {len(refused)} refused)"
              " — advisory, never blocks")
        rc = EXIT_WARN
    else:
        verdict = "pass"
        print(f"media-gate: verdict pass — all {len(verified)} line(s) match")
        rc = EXIT_OK

    log_record({
        "op": "media-gate",
        "target": target,
        "provider": "+".join(sorted(legs)) or GEN1_RAW_TAG,
        "task": "media_gate",
        "mode": mode,
        "input_sha256": _sha16(" ".join(u["text"] for u in units if u["text"])),
        "lines": [{k: v for k, v in vv.items() if not k.startswith("_")} for vv in verified],
        "refused": refused,
        "verdict": verdict,
        "escalated": False,
        "note": f"{len(verified)} verified, {len(refused)} refused, {gap_lines} with gaps",
    })
    return rc


# ── C4: record-asr — ASR accuracy against pinned ground truth ────────────────

ASR_TASK = "asr_roundtrip"
ASR_HEAD = "asr_faithful"
# pinned fixture sha256s — asserted in TestGen1Lane so the regression never
# depends on a sibling's working tree
NAR_S1_MP3_SHA256 = "167cf7b4f62e1699c1c83607f7e36fbf2d16c185ddfacafdb404b7968665af6b"
NAR_S1_TXT_SHA256 = "883166ae341158000c9740f126f5220b16c3c58963f72e36460cc8f8625ec37f"


def _append_feedback_row(row: dict) -> None:
    """Append one feedback-store row (same file and JSONL shape cmd_feedback
    writes, with the graded keys the calibration join reads)."""
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _roundtrip_verdict(cmp_: dict) -> str:
    """The machine's claim for one round-trip: faithful only when the token
    sequence agrees exactly (StepFun normalizes punctuation, so the bar is
    token-level, never byte-exact)."""
    return "faithful" if (cmp_["missing"] == 0 and cmp_["extra"] == 0) else "gaps"


def cmd_record_asr(args: argparse.Namespace) -> int:
    """C4: round-trip the pinned fixture(s) through the ASR wire and grade
    against ground truth with C3's comparator — the same function, not a copy.
    Writes a feedback-store row keyed (task "asr_roundtrip", head
    "asr_faithful", provider "gen1_raw/<leg>") — the join key the calibration
    command already reads — and prints the per-provider accuracy report."""
    fixtures_dir = Path(args.fixtures) if getattr(args, "fixtures", None) else FIXTURES_DIR
    pairs: list[tuple[Path, Path]] = []
    for audio in sorted(fixtures_dir.glob("*.mp3")):
        txt = audio.with_suffix(".txt")
        if not txt.is_file():
            print(f"record-asr: {audio.name} has no ground-truth .txt — skipped")
            continue
        pairs.append((audio, txt))
    if not pairs:
        print(f"record-asr: no .mp3/.txt fixture pairs in {fixtures_dir}")
        return EXIT_ERROR

    rows: list[dict] = []
    for audio_path, txt_path in pairs:
        try:
            audio = audio_path.read_bytes()
            truth = txt_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"record-asr: refused — {audio_path.name}: unreadable ({e})")
            continue
        res = media_transcribe(audio)
        if not res.get("ok"):
            print(f"record-asr: refused — {audio_path.name}: {res.get('error')}")
            continue
        cmp_ = compare_text(truth, res.get("transcript") or "")
        rows.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "input_sha256": hashlib.sha256(audio).hexdigest(),
            "task": ASR_TASK,
            "head_id": ASR_HEAD,
            "provider": leg_tag(res.get("result")),
            "label": "faithful",
            "predicted": _roundtrip_verdict(cmp_),
            "predicted_confidence": cmp_["agreement"],
            "actual": "faithful",
            "note": (f"round-trip {audio_path.name} agreement={cmp_['agreement']:.3f} "
                     f"missing={cmp_['missing']} extra={cmp_['extra']} "
                     f"tokens={cmp_['ref_tokens']}/{cmp_['got_tokens']}"),
        })

    for row in rows:
        _append_feedback_row(row)
        print(f"record-asr: {row['note']} → {row['provider']}")

    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["provider"], []).append(row)
    print(f"record-asr: {len(rows)} graded row(s) → {FEEDBACK_FILE}")
    for tag in sorted(groups):
        group = groups[tag]
        n = len(group)
        correct = sum(1 for r in group if str(r["predicted"]) == str(r["actual"]))
        confs = [float(r["predicted_confidence"] or 0.0) for r in group]
        print(f"  {tag}: n={n} accuracy={correct / n:.3f} "
              f"mean_agreement={sum(confs) / n:.3f}")
    if not rows:
        print("record-asr: no graded rows — every fixture refused (keys missing?)")
        return EXIT_ERROR
    return EXIT_OK


# ── C5: record-media-runs — telemetry → media-seconds table (no model) ───────

DEFAULT_TELEMETRY = Path.home() / ".config" / "gen1" / "telemetry.jsonl"
MEDIA_RUNS_COLUMNS = ["recorded_at", "provider", "model", "duration_seconds",
                      "request_id", "input_sha256", "input_chars", "input_bytes",
                      "line_sha"]


def _telemetry_path(override: str | None) -> Path:
    """The sink gen1 writes: --telemetry override, GEN1_TELEMETRY_FILE, else
    gen1's default (~/.config/gen1/telemetry.jsonl)."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get("GEN1_TELEMETRY_FILE")
    if env:
        return Path(env).expanduser()
    return DEFAULT_TELEMETRY


def cmd_record_media_runs(args: argparse.Namespace) -> int:
    """C5: parse gen1's telemetry JSONL into the tabular lane's tables dir —
    no model call (the record-runs shape). One row per attempt, failed legs
    included; a corrupt line is skipped by name, never fatal. The sink carries
    no timestamp, so each row is stamped with its ingestion date (UTC): run
    daily for honest daily bands. Idempotent — a line already ingested (matched
    by its content sha) is a duplicate, not a second charge."""
    path = _telemetry_path(getattr(args, "telemetry", None))
    if not path.is_file():
        print(f"record-media-runs: no telemetry at {path} — run a speak/transcribe first")
        return EXIT_ERROR

    _, existing = read_table(TABLES_DIR / MEDIA_RUNS_TABLE)
    seen = {str(r.get("line_sha", "")) for r in existing}
    # The sink is append-only and failed legs often land byte-identical rows
    # (no request_id, duration 0.0) — each occurrence is a distinct attempt, so
    # the ingest key is content sha + occurrence index. Re-running over an
    # unchanged file reproduces the same keys and lands nothing twice.
    occurrences: dict[str, int] = {}
    today = datetime.now(timezone.utc).date().isoformat()
    new_rows: list[dict] = []
    skipped: list[str] = []
    duplicates = 0
    total = 0
    digest = hashlib.sha256()
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            digest.update(raw.encode("utf-8", errors="replace"))
            total += 1
            base_sha = hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()[:16]
            n_seen = occurrences.get(base_sha, 0) + 1
            occurrences[base_sha] = n_seen
            line_sha = f"{base_sha}#{n_seen}"
            if line_sha in seen:
                duplicates += 1
                continue
            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError("row is not an object")
                row = {
                    "recorded_at": today,
                    "provider": str(obj.get("provider", "")),
                    "model": str(obj.get("model", "")),
                    "duration_seconds": float(obj.get("duration_seconds", 0.0) or 0.0),
                    "request_id": str(obj.get("request_id") or ""),
                    "input_sha256": str(obj.get("input_sha256", "")),
                    "input_chars": int(obj.get("input_chars", 0) or 0),
                    "input_bytes": int(obj.get("input_bytes", 0) or 0),
                    "line_sha": line_sha,
                }
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                skipped.append(f"line {lineno}: {e}")
                continue
            seen.add(line_sha)
            new_rows.append(row)

    if new_rows:
        write_table(TABLES_DIR / MEDIA_RUNS_TABLE, MEDIA_RUNS_COLUMNS, existing + new_rows)

    print(f"record-media-runs: {total} line(s) parsed from {path}")
    print(f"  + {len(new_rows)} new row(s) → {TABLES_DIR / MEDIA_RUNS_TABLE}"
          + (f", {duplicates} duplicate(s) already ingested" if duplicates else ""))
    for s in skipped:
        print(f"  - skipped {s}")

    if new_rows or duplicates:
        verdict = "ok"
        rc = EXIT_OK
    else:
        verdict = "error"
        rc = EXIT_ERROR
        print("record-media-runs: nothing recorded" +
              (" (every line corrupt)" if skipped else " (sink empty)"))
    log_record({
        "op": "record-media-runs",
        "target": str(path),
        "provider": "+".join(sorted({r["provider"] for r in new_rows})) or "none",
        "task": "media_runs",
        "input_sha256": digest.hexdigest(),
        "lines_total": total,
        "rows_new": len(new_rows),
        "rows_duplicate": duplicates,
        "rows_skipped": len(skipped),
        "verdict": verdict,
        "escalated": False,
    })
    return rc


# ── C6: media-budget — forecast daily media-seconds bands per provider ───────

MEDIA_BUDGET_TASK = "media_budget_forecast"


def _daily_series(rows: list[dict]) -> dict[str, dict[str, float]]:
    """provider → {date → summed duration_seconds}. The sink carries no
    timestamp; recorded_at is the ingestion date, so daily bands need a daily
    record-media-runs cadence."""
    series: dict[str, dict[str, float]] = {}
    for r in rows:
        day = str(r.get("recorded_at", ""))[:10]
        provider = str(r.get("provider", ""))
        if not day or not provider:
            continue
        try:
            dur = float(r.get("duration_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        series.setdefault(provider, {}).setdefault(day, 0.0)
        series[provider][day] += dur
    return series


def _recorded_stats(days: list[str], values: list[float]) -> dict:
    """The recorded-table fallback: what history says, with no band attached."""
    return {"days": len(values), "mean_daily": round(sum(values) / len(values), 3),
            "latest_day": days[-1], "latest": values[-1],
            "total": round(sum(values), 3)}


def cmd_media_budget(args: argparse.Namespace) -> int:
    """C6: forecast daily media-seconds bands per provider through the existing
    forecast_band (task media_budget_forecast), compare the next-day estimate
    against a requested budget, and degrade to the recorded table with a named
    reason when sdm1 is unavailable or history is short — never a crash, never
    a fabricated band. Batch-only (the tabular lesson)."""
    table_path = TABLES_DIR / MEDIA_RUNS_TABLE
    if not table_path.is_file():
        print(f"media-budget: no {MEDIA_RUNS_TABLE} at {TABLES_DIR} — run record-media-runs first")
        return EXIT_ERROR
    _, rows = read_table(table_path)
    if not rows:
        print(f"media-budget: {MEDIA_RUNS_TABLE} is empty — run record-media-runs first")
        return EXIT_ERROR

    budget = getattr(args, "budget_seconds", None)
    series = _daily_series(rows)
    print(f"media-budget: {len(rows)} recorded run(s) across {len(series)} provider(s)"
          f" — {table_path}")
    bands: dict[str, dict] = {}
    degraded: list[str] = []
    estimate_total = 0.0
    for provider in sorted(series):
        days = sorted(series[provider])
        values = [round(series[provider][d], 3) for d in days]
        if len(values) < 4:
            reason = f"only {len(values)} recorded day(s) — forecast needs 4"
            degraded.append(f"{provider}: {reason}")
            estimate_total += values[-1]
            stats = _recorded_stats(days, values)
            print(f"  {provider}: mean {stats['mean_daily']:.1f}s/day over "
                  f"{stats['days']} day(s), latest {stats['latest_day']} "
                  f"{stats['latest']:.1f}s — no band ({reason})")
            continue
        result = forecast_band(values, ahead=1, task_id=MEDIA_BUDGET_TASK)
        points = result.get("points") or []
        if not result.get("ok") or not points:
            reason = result.get("error") or "no band returned"
            degraded.append(f"{provider}: {reason}")
            estimate_total += values[-1]
            stats = _recorded_stats(days, values)
            print(f"  {provider}: forecast declined — {reason}; recorded mean "
                  f"{stats['mean_daily']:.1f}s/day over {stats['days']} day(s) "
                  f"shown instead")
            continue
        pt = points[0]
        bands[provider] = pt
        estimate_total += float(pt["median"])
        print(f"  {provider}: next-day band {pt['lo']:.1f}–{pt['hi']:.1f}s "
              f"(median {pt['median']:.1f}s) from {len(values)} days")

    verdict = "pass"
    if budget is not None:
        over = estimate_total > float(budget)
        verdict = "over" if over else "pass"
        print(f"  budget: next-day estimate {estimate_total:.1f}s vs "
              f"{float(budget):.1f}s → {'OVER' if over else 'within'}")
    if degraded:
        print("  degraded (recorded stats shown, no fabricated band):")
        for reason in degraded:
            print(f"    - {reason}")

    log_record({
        "op": "media-budget",
        "target": str(table_path),
        "provider": "+".join(sorted(bands)) or "none",
        "task": MEDIA_BUDGET_TASK,
        "input_sha256": _sha16(str(sorted((r.get("provider", ""), str(r.get("duration_seconds", "")))
                                          for r in rows))),
        "bands": {p: {"median": pt["median"], "lo": pt["lo"], "hi": pt["hi"]}
                  for p, pt in bands.items()},
        "estimate_total_s": round(estimate_total, 3),
        "budget_s": float(budget) if budget is not None else None,
        "degraded": degraded,
        "verdict": verdict,
        "escalated": verdict == "over",
    })
    return EXIT_WARN if verdict == "over" else EXIT_OK


# ── utility verbs: one accountable log row each (batch-only) ─────────────────


def cmd_media_transcribe(args: argparse.Namespace) -> int:
    """Transcribe one audio file through gen1. The transcript goes to stdout;
    the log row carries counts and identifiers, never content."""
    try:
        audio = Path(args.audio).read_bytes()
    except OSError as e:
        print(f"media-transcribe: cannot read {args.audio}: {e}")
        return EXIT_ERROR
    res = media_transcribe(audio, language=getattr(args, "language", None),
                           provider=getattr(args, "provider", None))
    if not res.get("ok"):
        print(f"media-transcribe: refused — {res.get('error')}")
        log_record({"op": "media-transcribe", "target": str(args.audio),
                    "provider": GEN1_RAW_TAG, "task": "media_transcribe",
                    "input_sha256": hashlib.sha256(audio).hexdigest(),
                    "verdict": "refused", "escalated": False})
        return EXIT_ERROR
    result = res.get("result")
    print(res.get("transcript") or "")
    log_record({"op": "media-transcribe", "target": str(args.audio),
                "provider": leg_tag(result), "task": "media_transcribe",
                "input_sha256": hashlib.sha256(audio).hexdigest(),
                "duration_seconds": getattr(result, "duration_seconds", None),
                "request_id": (result.telemetry.request_id if result.telemetry else None),
                "verdict": "ok", "escalated": False})
    return EXIT_OK


def cmd_media_speak(args: argparse.Namespace) -> int:
    """Render text to speech through gen1 into --out. One accountable log row
    (provider, duration measured from the returned bytes, request_id) — the
    render itself is never judged."""
    if bool(args.text) == bool(args.text_file):
        print("usage: media-speak (--text T | --text-file F) --out FILE")
        return EXIT_ERROR
    try:
        text = args.text if args.text else Path(args.text_file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"media-speak: cannot read --text-file: {e}")
        return EXIT_ERROR
    if not text.strip():
        print("media-speak: empty text — nothing to render")
        return EXIT_ERROR
    res = media_speak(text, voice=getattr(args, "voice", None),
                      language=getattr(args, "language", "en") or "en",
                      format=getattr(args, "format", None),
                      provider=getattr(args, "provider", None),
                      speed=getattr(args, "speed", None),
                      instruction=getattr(args, "instruction", None))
    if not res.get("ok"):
        print(f"media-speak: refused — {res.get('error')}")
        log_record({"op": "media-speak", "target": str(args.out),
                    "provider": GEN1_RAW_TAG, "task": "media_speak",
                    "input_sha256": _sha16(text), "verdict": "refused",
                    "escalated": False})
        return EXIT_ERROR
    result = res.get("result")
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(res.get("audio") or b"")
    except OSError as e:
        print(f"media-speak: cannot write {out}: {e}")
        return EXIT_ERROR
    print(f"media-speak: {out} — {result.duration_seconds:.2f}s "
          f"{result.format} @{result.sample_rate}Hz, voice={result.voice}")
    log_record({"op": "media-speak", "target": str(out),
                "provider": leg_tag(result), "task": "media_speak",
                "input_sha256": _sha16(text),
                "duration_seconds": result.duration_seconds,
                "format": result.format, "sample_rate": result.sample_rate,
                "request_id": result.request_id,
                "verdict": "ok", "escalated": False})
    return EXIT_OK


def cmd_media_imagine(args: argparse.Namespace) -> int:
    """Render image(s) through gen1 into --out. One accountable log row — the
    render itself is never judged."""
    if bool(args.prompt) == bool(args.prompt_file):
        print("usage: media-imagine (--prompt P | --prompt-file F) --out DIR")
        return EXIT_ERROR
    try:
        prompt = args.prompt if args.prompt else Path(args.prompt_file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"media-imagine: cannot read --prompt-file: {e}")
        return EXIT_ERROR
    if not prompt.strip():
        print("media-imagine: empty prompt — nothing to render")
        return EXIT_ERROR
    res = media_imagine(prompt, aspect_ratio=getattr(args, "aspect_ratio", "1:1") or "1:1",
                        n=getattr(args, "n", 1) or 1,
                        provider=getattr(args, "provider", None))
    if not res.get("ok"):
        print(f"media-imagine: refused — {res.get('error')}")
        log_record({"op": "media-imagine", "target": str(args.out),
                    "provider": GEN1_RAW_TAG, "task": "media_imagine",
                    "input_sha256": _sha16(prompt), "verdict": "refused",
                    "escalated": False})
        return EXIT_ERROR
    result = res.get("result")
    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for i, img in enumerate(result.images, start=1):
            dest = out_dir / f"image-{i:02d}.{img.mime.split('/')[-1]}"
            dest.write_bytes(img.data)
            written.append(f"{dest} ({img.w}x{img.h})")
    except OSError as e:
        print(f"media-imagine: cannot write into {out_dir}: {e}")
        return EXIT_ERROR
    for w in written:
        print(f"media-imagine: {w}")
    log_record({"op": "media-imagine", "target": str(out_dir),
                "provider": leg_tag(result), "task": "media_imagine",
                "input_sha256": _sha16(prompt),
                "images": len(result.images),
                "request_id": (result.telemetry.request_id if result.telemetry else None),
                "verdict": "ok", "escalated": False})
    return EXIT_OK
