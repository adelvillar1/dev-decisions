"""Tabular decision lane: sdm1/TabPFN-hosted backed batch commands.

The second model class under the same accountability machinery. Where the
sys1 lane judges what the work SAYS (diffs, plans, logs, docs), this lane
scores what the work MEASURES: run histories, benchmark series, the JSONL +
feedback calibration stores, git-history features, fleet metric tables.

Hard rule (2026-10-07 plan): measured small-task latency is ~160s, so no
command here may run a live model call inside a synchronous hook path.
Hooks (pre-push) read cached tables produced by these batch commands.

Every command is stdlib-only; the model call goes through the sdm1 library
(bootstrapped in judgment.py, provider id "tabpfn-hosted"). Fail-open: an
unavailable sdm1 degrades the command to its mechanical layer or an
actionable error row — never a crash, never fabricated verdicts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import EXIT_ERROR, EXIT_OK, EXIT_WARN, LOG_DIR, log_record
from .judgment import sdm1

# section: tabular

TABLES_DIR = Path.home() / ".local" / "share" / "dev-decisions" / "tables"
FEEDBACK_FILE = LOG_DIR / "feedback" / "feedback.jsonl"

# P(override) above this at a confidence point suggests the floor belongs higher
_OVERRIDE_P = 0.3
# confidence grid scored for per-head override probabilities
_CONF_GRID = (0.5, 0.6, 0.7, 0.8, 0.9)


# ── sdm1 glue ────────────────────────────────────────────────────────────────


def sdm1_ready() -> tuple[bool, str]:
    """(ready, reason) — library importable and the hosted backend registered."""
    if sdm1 is None:
        return False, "sdm1 not importable — set DEV_DECISIONS_SDM1_PATH or install ~/Projects/sdm1"
    try:
        if "tabpfn-hosted" in sdm1.registry.all_ids():
            return True, "sdm1 + tabpfn-hosted available"
        return False, "sdm1 predates the tabpfn-hosted provider — update ~/Projects/sdm1"
    except Exception as e:
        return False, f"sdm1 registry unreadable: {e}"


def tabular_classify(
    rows: list[dict],
    target: str,
    task_type: str,
    *,
    task_id: str,
    metadata: dict | None = None,
    table_name: str = "table",
    confidence_floor: float | None = None,
    telemetry: dict | None = None,
) -> dict:
    """One tabular classification through sdm1's tabpfn-hosted backend.

    Returns {ok, predictions, result, error}. Never raises — an unavailable
    or failing backend comes back as ok=False with an actionable reason
    (fail-open, mirroring the sys1 lane). Raw rows never leave the process
    except as model inputs to the hosted API itself; the JSONL row carries
    only shapes and hashes (sdm1's own redaction holds on its side).
    """
    ready, reason = sdm1_ready()
    if not ready:
        return {"ok": False, "predictions": [], "error": reason, "result": None}
    try:
        task = sdm1.TableTask(
            id=task_id,
            target=target,
            task_type=task_type,  # type: ignore[arg-type]
            confidence_floor=confidence_floor,
            metadata=dict(metadata or {}),
        )
        result = sdm1.classify(
            rows,
            task=task,
            model="tabpfn-hosted",
            table_name=table_name,
            log=True,
        )
    except Exception as e:
        return {"ok": False, "predictions": [], "error": f"{type(e).__name__}: {e}", "result": None}
    if telemetry is not None:
        telemetry.setdefault("sdm1", {}).update(
            {
                "billing_model_version": result.telemetry.get("billing_model_version"),
                "execution_mode": result.telemetry.get("execution_mode"),
                "cache_outcome": result.telemetry.get("cache_outcome"),
                "structured_ok": result.telemetry.get("structured_ok"),
                "verdict": result.verdict,
                "latency_ms": result.latency_ms,
            }
        )
    ok = result.verdict != "error" and bool(result.predictions)
    return {
        "ok": ok,
        "predictions": result.predictions,
        "result": result,
        "error": None if ok else (result.error or "no predictions"),
    }


# ── store readers ────────────────────────────────────────────────────────────


def read_jsonl_dir(log_dir: Path, pattern: str = "events.jsonl") -> list[dict]:
    rows: list[dict] = []
    if not log_dir.exists():
        return rows
    for f in sorted(log_dir.rglob(pattern)):
        try:
            for line in f.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
    return rows


def load_feedback_rows() -> list[dict]:
    if not FEEDBACK_FILE.exists():
        return []
    out = []
    for line in FEEDBACK_FILE.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def read_table(path: Path) -> tuple[list[str], list[dict]]:
    """Read a CSV table into (columns, rows). Missing file → ([], [])."""
    import csv

    if not path.exists():
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        return columns, list(reader)


def write_table(path: Path, columns: list[str], rows: list[dict]) -> None:
    """Write (overwriting) a CSV table under TABLES_DIR; mkdir -p first."""
    import csv

    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ── C1: text-lane routing bridge ─────────────────────────────────────────────


def classify_text_via_sdm1(task: str, diff: str, cfg: dict) -> dict:
    """Route a text-lane task through sdm1 (by_task override target "sdm1").

    Privacy-first: the diff text never leaves the process. The tabular lane
    scores STRUCTURAL features only, calibrated by graded feedback rows for
    the task. Today no graded store carries those features yet, so the lane
    DECLINES instead of guessing — declines are first-class answers (the
    sys1 lesson) and the caller escalates to the human/text-lane path.
    Returns the same outcome dict shape as _sys1_classify.
    """
    from .judgment import _diff_paths

    ready, reason = sdm1_ready()
    t0 = time.monotonic()
    del _diff_paths  # structural feature extraction lands with the first graded store
    return {
        "results": {},
        "providers_used": ["sdm1"],
        "telemetry": {
            "sdm1": {
                "ready": ready,
                "declined": True,
                "reason": (
                    reason
                    if not ready
                    else "no labeled tabular context for this task — the sdm1 text lane "
                    "needs graded rows carrying structural features; escalate to text-lane providers"
                ),
            }
        },
        "escalated": True,
        "summary": ["sdm1 (tabular lane): declined — no labeled context; escalate"],
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "verdict": "escalate",
    }


# ── C2: override-prior — gate the gate ───────────────────────────────────────


def _override_training_rows(feedback_rows: list[dict], events: list[dict]) -> list[dict]:
    """One graded feature row per (feedback x gate event) join, by input_sha256.

    The feedback store carries the ground truth (label/actual); the predicted
    label + confidence live in the matching gate EVENT row's heads. Features
    are identifiers and confidences only — no input text ever joins the
    table (hosted-model inputs stay privacy-safe). Rows without a join or a
    decidable predicted label are skipped: no invented evidence.
    """
    ev_index: dict[str, dict] = {}
    for e in events:
        sha = e.get("input_sha256")
        if sha and isinstance(e.get("heads"), dict):
            ev_index.setdefault(str(sha), e)
    rows: list[dict] = []
    for fb in feedback_rows:
        actual = fb.get("actual")
        if actual is None:
            actual = fb.get("label")
        if actual is None:
            continue
        e = ev_index.get(str(fb.get("input_sha256") or ""))
        if not e:
            continue
        heads = e.get("heads") or {}
        prov = str(fb.get("provider") or "?")
        flat = heads.get(prov) if isinstance(heads.get(prov), dict) else heads
        if not isinstance(flat, dict):
            continue
        a = flat.get(fb.get("head_id"))
        if not isinstance(a, dict):
            continue
        predicted = a.get("label")
        conf = a.get("confidence")
        if predicted is None and isinstance(a.get("noul"), (int, float)):
            predicted = "yes" if a["noul"] >= 0.5 else "no"
            conf = a["noul"]
        if predicted is None:
            continue
        rows.append(
            {
                "provider": prov,
                "task": str(fb.get("task") or "?"),
                "head_id": str(fb.get("head_id") or "?"),
                "confidence": float(conf) if isinstance(conf, (int, float)) else 0.5,
                "overridden": "no" if str(predicted) == str(actual) else "yes",
            }
        )
    return rows


def build_override_groups(training_rows: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Aggregate graded rows per (task, head_id, provider): n, override count."""
    groups: dict[tuple[str, str, str], dict] = {}
    for r in training_rows:
        key = (r["task"], r["head_id"], r["provider"])
        g = groups.setdefault(key, {"n": 0, "overrides": 0, "confs": []})
        g["n"] += 1
        g["overrides"] += 1 if r["overridden"] == "yes" else 0
        g["confs"].append(r["confidence"])
    return groups


def _sdm1_override_probabilities(
    training_rows: list[dict], query_rows: list[dict], telemetry: dict
) -> dict[tuple[int, int], float]:
    """Score (query_index, conf_grid_index) → P(overridden) via sdm1.

    The caller has already decided the model is worth calling; the model's
    probabilities smooth per-head override rates across neighboring
    (provider, task, head, confidence) points. Returns {} when sdm1 is
    unavailable — the caller falls back to observed rates.
    """
    table = []
    for r in training_rows:
        table.append(dict(r))  # labeled context (features + overridden label)
    for qi, q in enumerate(query_rows):
        for ci, conf in enumerate(_CONF_GRID):
            row = dict(q)
            row["confidence"] = conf
            row["overridden"] = None
            table.append(row)
    out = tabular_classify(
        table,
        target="overridden",
        task_type="classification",
        task_id="override_prior",
        table_name="override-prior",
        telemetry=telemetry,
    )
    probs: dict[tuple[int, int], float] = {}
    if not out["ok"]:
        return probs
    for p in out["predictions"]:
        qi, ci = divmod(p.row_index, len(_CONF_GRID))
        if p.probabilities and "yes" in p.probabilities:
            probs[(qi, ci)] = float(p.probabilities["yes"])
    return probs


def rank_override_heads(
    query_keys: list[tuple[str, str, str]],
    groups: dict[tuple[str, str, str], dict],
    probs: dict[tuple[int, int], float],
    *,
    model_used: bool,
) -> list[dict]:
    """Rank heads by override evidence; pure and unit-tested (C2 fixture).

    With modeled probabilities, per-head override probs come from the sdm1
    scoring grid; without, the observed rate repeats across the grid (an
    honest mechanical fallback — never a fabricated model number).
    """
    ranked = []
    for qi, (task, head, prov) in enumerate(query_keys):
        g = groups[(task, head, prov)]
        observed_rate = g["overrides"] / g["n"]
        point_probs = [
            probs.get((qi, ci), observed_rate) for ci in range(len(_CONF_GRID))
        ] if model_used else [observed_rate] * len(_CONF_GRID)
        suggested = 0.9
        for conf, p_ov in zip(_CONF_GRID, point_probs, strict=False):
            if p_ov <= _OVERRIDE_P:
                suggested = conf
                break
        ranked.append(
            {
                "task": task,
                "head_id": head,
                "provider": prov,
                "n": g["n"],
                "observed_override_rate": round(observed_rate, 3),
                "override_probs": {str(c): round(p, 3) for c, p in zip(_CONF_GRID, point_probs, strict=False)},
                "suggested_floor": suggested,
            }
        )
    ranked.sort(key=lambda r: (-r["observed_override_rate"], r["task"], r["head_id"]))
    return ranked


def cmd_override_prior(args: argparse.Namespace) -> int:
    """Per-head override probabilities + suggested floors from the stores."""
    events = read_jsonl_dir(LOG_DIR)
    feedback_rows = load_feedback_rows()
    training_rows = _override_training_rows(feedback_rows, events)
    groups = build_override_groups(training_rows)
    if not groups:
        print("No joined override evidence yet — grade gate verdicts with:")
        print("  dev-decisions disposition <gate> <target> --status ...")
        return EXIT_OK

    query_keys = sorted(groups.keys())
    query_rows = [
        {
            "provider": prov,
            "task": task,
            "head_id": head,
            "confidence": sum(groups[(task, head, prov)]["confs"]) / len(groups[(task, head, prov)]["confs"]),
        }
        for (task, head, prov) in query_keys
    ]

    telemetry: dict = {}
    probs = _sdm1_override_probabilities(training_rows, query_rows, telemetry)
    model_used = bool(probs)

    ranked = rank_override_heads(query_keys, groups, probs, model_used=model_used)

    print(
        f"override-prior: {len(ranked)} heads graded "
        f"(model: {'sdm1/tabpfn-hosted' if model_used else 'observed rates — sdm1 unavailable or thin'})"
    )
    print(f"  {'task':<18} {'head':<28} {'provider':<10} {'n':>4} {'rate':>6} {'floor?':>7}")
    for r in ranked[: max(1, args.top)]:
        print(
            f"  {r['task'][:18]:<18} {r['head_id'][:28]:<28} {r['provider'][:10]:<10} "
            f"{r['n']:>4} {r['observed_override_rate']:>6.2f} {r['suggested_floor']:>7.2f}"
        )
    if not model_used:
        print("  (sdm1 unavailable — probabilities are observed rates; install ~/Projects/sdm1 for modeled priors)")

    log_record(
        {
            "op": "override-prior",
            "target": "override-prior",
            "provider": "sdm1" if model_used else "mechanical",
            "providers_used": ["sdm1"] if model_used else [],
            "input_sha256": hashlib.sha256(
                json.dumps(sorted(str(g) for g in query_keys)).encode()
            ).hexdigest()[:16],
            "heads": {},
            "verdict": "pass",
            "escalated": False,
            "n_heads": len(ranked),
            "top_overridden": ranked[0]["head_id"] if ranked else None,
            "telemetry": telemetry,
        }
    )
    if args.json:
        print(json.dumps(ranked, indent=2))
    return EXIT_OK


# ── C3: record-runs — CI history ingest ──────────────────────────────────────


def _gh_json(gh_args: list[str]) -> str:
    proc = subprocess.run(
        ["gh", *gh_args], capture_output=True, text=True, timeout=120, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh failed").strip()[:300])
    return proc.stdout


def parse_gh_runs(runs_json: str, repo: str) -> list[dict]:
    """Normalize `gh run list --json` output into table rows (pure; unit-tested)."""
    try:
        runs = json.loads(runs_json)
    except Exception:
        return []
    rows = []
    for run in runs if isinstance(runs, list) else []:
        try:
            rows.append(
                {
                    "repo": repo,
                    "run_id": str(run.get("databaseId") or ""),
                    "check_name": str(run.get("workflowName") or run.get("name") or ""),
                    "conclusion": str(run.get("conclusion") or run.get("status") or ""),
                    "event": str(run.get("event") or ""),
                    "branch": str(run.get("headBranch") or ""),
                    "created_at": str(run.get("createdAt") or ""),
                }
            )
        except Exception:
            continue
    return [r for r in rows if r["run_id"]]


def parse_gh_jobs(jobs_json: str, repo: str, run_row: dict) -> list[dict]:
    """Normalize `gh run view --json jobs` job entries into check-level rows."""
    try:
        payload = json.loads(jobs_json)
    except Exception:
        return []
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    rows = []
    for job in jobs if isinstance(jobs, list) else []:
        try:
            rows.append(
                {
                    "repo": repo,
                    "run_id": run_row["run_id"],
                    "check_name": str(job.get("name") or ""),
                    "conclusion": str(job.get("conclusion") or ""),
                    "event": run_row["event"],
                    "branch": run_row["branch"],
                    "created_at": run_row["created_at"],
                }
            )
        except Exception:
            continue
    return [r for r in rows if r["check_name"]]


def cmd_record_runs(args: argparse.Namespace) -> int:
    """Ingest CI run history per repo into an idempotent CSV check table."""
    if shutil.which("gh") is None:
        print("error: gh CLI required (https://cli.github.com/)", file=sys.stderr)
        return EXIT_ERROR
    repos = [r.strip() for r in (args.repos or "").split(",") if r.strip()]
    if not repos:
        repos = [Path(".").resolve().name]
        owner_repo = _guess_owner_repo()
        if owner_repo:
            repos = [owner_repo]
    table_path = TABLES_DIR / "ci_runs.csv"
    columns = ["repo", "run_id", "check_name", "conclusion", "event", "branch", "created_at"]
    _, existing = read_table(table_path)
    seen = {(r.get("repo"), r.get("run_id"), r.get("check_name")) for r in existing}
    all_rows = list(existing)
    added = 0
    for repo in repos:
        try:
            runs_raw = _gh_json(
                ["run", "list", "-R", repo, "--limit", str(args.limit),
                 "--json", "databaseId,workflowName,name,conclusion,event,headBranch,createdAt"]
            )
            run_rows = parse_gh_runs(runs_raw, repo)
        except RuntimeError as e:
            print(f"  {repo}: gh failed — {e}")
            continue
        new_runs = [r for r in run_rows if (r["repo"], r["run_id"], r["check_name"]) not in seen]
        if args.jobs:
            for run_row in new_runs[: max(1, args.job_runs)]:
                try:
                    jobs_raw = _gh_json(["run", "view", run_row["run_id"], "-R", repo, "--json", "jobs"])
                    for job_row in parse_gh_jobs(jobs_raw, repo, run_row):
                        key = (job_row["repo"], job_row["run_id"], job_row["check_name"])
                        if key not in seen:
                            all_rows.append(job_row)
                            seen.add(key)
                            added += 1
                except RuntimeError as e:
                    print(f"  {repo} run {run_row['run_id']}: jobs fetch failed — {e}")
                    continue
        for run_row in new_runs:
            key = (run_row["repo"], run_row["run_id"], run_row["check_name"])
            if key not in seen:
                all_rows.append(run_row)
                seen.add(key)
                added += 1
        print(f"  {repo}: {len(run_rows)} runs fetched")
    write_table(table_path, columns, all_rows)
    print(f"table: {table_path} ({len(all_rows)} rows, +{added} new)")
    log_record(
        {
            "op": "record-runs",
            "repo": ",".join(repos),
            "target": str(table_path),
            "rows_added": added,
            "verdict": "pass",
        }
    )
    return EXIT_OK


def _guess_owner_repo() -> str | None:
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        path = urllib.parse.urlparse(url).path if "://" in url else url.split(":", 1)[-1]
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1].removesuffix('.git')}"
    except Exception:
        return None
    return None


# ── C4: history-gate — flake probabilities from recorded tables ─────────────


def history_candidates(
    rows: list[dict], *, repo: str | None = None, min_runs: int = 5
) -> list[dict]:
    """Aggregate a check table per (repo, check_name); pure and unit-tested.

    A candidate is intermittent (0 < fail_rate < 1) with enough runs. Stable
    checks (rate 0 or 1) are mechanical: all-fail is a broken test, all-pass
    is green — neither is a flake judgment for the model.
    """
    FAILED = {"failure", "timed_out", "startup_failure"}
    counts: dict[tuple[str, str], dict] = {}
    for r in rows:
        if repo and r.get("repo") != repo:
            continue
        name = r.get("check_name") or ""
        conclusion = str(r.get("conclusion") or "").lower()
        if not name or conclusion in ("skipped", "cancelled", "", "pending", "in_progress", "queued"):
            continue
        key = (r.get("repo") or "?", name)
        g = counts.setdefault(key, {"runs": 0, "fails": 0})
        g["runs"] += 1
        g["fails"] += 1 if conclusion in FAILED else 0
    out = []
    for (rp, name), g in sorted(counts.items()):
        if g["runs"] < min_runs:
            continue
        rate = g["fails"] / g["runs"]
        out.append(
            {
                "repo": rp,
                "check_name": name,
                "n_runs": g["runs"],
                "fail_rate": round(rate, 4),
                "intermittent": 0.0 < rate < 1.0,
            }
        )
    return out


def _flake_context_rows() -> list[dict]:
    """Labeled context from graded history-gate dispositions, joined by sha.

    Feedback rows carry the ground truth (label); the check's mechanical
    features (fail_rate, n_runs) live in the matched history-gate event row's
    flags. No join → no row (no invented evidence).
    """
    ev_index: dict[str, dict] = {}
    for e in read_jsonl_dir(LOG_DIR):
        if e.get("op") != "history-gate":
            continue
        sha = e.get("input_sha256")
        if sha and e.get("flags"):
            ev_index.setdefault(str(sha), e)
    rows = []
    for fb in load_feedback_rows():
        if fb.get("task") != "history-gate":
            continue
        actual = fb.get("actual") or fb.get("label")
        if actual is None:
            continue
        e = ev_index.get(str(fb.get("input_sha256") or ""))
        if not e:
            continue
        for flag in e.get("flags") or []:
            if not flag.get("fail_rate"):
                continue
            rows.append(
                {
                    "fail_rate": float(flag["fail_rate"]),
                    "n_runs": float(flag.get("n_runs") or 0),
                    "flake": str(actual),
                }
            )
    return rows


def cmd_history_gate(args: argparse.Namespace) -> int:
    """Flag intermittent checks; sdm1 ranks them when graded labels exist."""
    table_path = Path(args.table) if args.table else TABLES_DIR / "ci_runs.csv"
    _, rows = read_table(table_path)
    if not rows:
        print(f"No check history at {table_path} — run `dev-decisions record-runs` first.")
        return EXIT_ERROR
    candidates = history_candidates(rows, repo=args.repo, min_runs=args.min_runs)
    intermittent = [c for c in candidates if c["intermittent"]]

    telemetry: dict = {}
    predictions = {}
    if intermittent:
        context = _flake_context_rows()
        if len(context) >= args.min_labels:
            table = [
                *context,
                *[
                    {
                        "fail_rate": c["fail_rate"],
                        "n_runs": float(c["n_runs"]),
                        "flake": None,
                        "_actual": None,
                    }
                    for c in intermittent
                ],
            ]
            out = tabular_classify(
                table,
                target="flake",
                task_type="classification",
                task_id="history_flake",
                table_name="history-gate",
                confidence_floor=args.floor,
                telemetry=telemetry,
            )
            if out["ok"]:
                for p in out["predictions"]:
                    predictions[p.row_index] = p
            else:
                print(f"  (sdm1 scoring unavailable: {out['error']} — mechanical flags only)")
        else:
            print(
                f"  (cold start: {len(context)} graded history rows < {args.min_labels} — "
                "mechanical flags only; disposition history-gate flags to grow the label set)"
            )

    flags = []
    lines = [f"history-gate: {len(candidates)} checks analyzed ({len(intermittent)} intermittent)"]
    for i, c in enumerate(intermittent):
        p = predictions.get(i)
        prob = p.probabilities.get("yes") if p and p.probabilities else None
        conf = p.confidence if p else None
        lines.append(
            f"  FLAG {c['repo']}/{c['check_name']}: fail_rate={c['fail_rate']:.2f} n={c['n_runs']}"
            + (f" p_flake={prob:.2f}" if prob is not None else "")
        )
        flags.append(
            {
                **c,
                "p_flake": round(prob, 4) if prob is not None else None,
                "confidence": round(conf, 4) if conf is not None else None,
            }
        )
    for c in candidates:
        if not c["intermittent"]:
            if args.show_stable:
                lines.append(f"  ok   {c['repo']}/{c['check_name']}: fail_rate={c['fail_rate']:.2f} n={c['n_runs']}")

    verdict = "escalated" if flags else "pass"
    print("\n".join(lines) or "history-gate: no checks analyzed")
    sha = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]
    log_record(
        {
            "op": "history-gate",
            "repo": args.repo or "all",
            "target": str(table_path),
            "provider": "sdm1" if predictions else "mechanical",
            "providers_used": ["sdm1"] if predictions else [],
            "input_sha256": sha,
            "heads": {"flake": {"label": "yes" if flags else "no",
                                 "confidence": flags[0]["confidence"] if flags else None}},
            "flags": flags,
            "verdict": verdict,
            "escalated": bool(flags),
            "telemetry": telemetry,
        }
    )
    return EXIT_WARN if flags else EXIT_OK


# ── C5: record-bench + budget-gate ───────────────────────────────────────────


def cmd_record_bench(args: argparse.Namespace) -> int:
    """Time one command run and append a row to the benchmark table."""
    if not args.cmd:
        print("error: no command given (use: record-bench <name> -- <cmd...>)", file=sys.stderr)
        return EXIT_ERROR
    t0 = time.monotonic()
    proc = subprocess.run(args.cmd, check=False)
    duration = time.monotonic() - t0
    commit = ""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except Exception:
        pass
    table_path = TABLES_DIR / f"bench_{args.name}.csv"
    _, rows = read_table(table_path)
    rows.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "name": args.name,
            "duration_s": round(duration, 4),
            "commit": commit,
        }
    )
    write_table(table_path, ["ts", "name", "duration_s", "commit"], rows)
    print(f"recorded: {duration:.3f}s → {table_path} (exit {proc.returncode})")
    return EXIT_OK


def forecast_band(
    series: list[float], *, ahead: int = 1, telemetry: dict | None = None
) -> dict:
    """Forecast the next point(s) of a series via sdm1; pure input, banded output.

    Context rows: t, prev, value (labeled history). Query rows: the final
    `ahead` points re-forecast with their target withheld. Returns
    {ok, points: [{index, median, lo, hi, actual}], error}.
    """
    if len(series) < 4:
        return {"ok": False, "points": [], "error": f"series too short ({len(series)} rows, need >= 4)"}
    ahead = max(1, min(ahead, len(series) - 3))
    table = []
    n = len(series)
    for i, v in enumerate(series):
        table.append({"t": i, "prev": series[i - 1] if i else v, "value": v})
    for i in range(n - ahead, n):
        table[i]["value"] = None
    out = tabular_classify(
        table,
        target="value",
        task_type="forecast",
        task_id="budget_forecast",
        table_name="budget-gate",
        telemetry=telemetry,
    )
    if not out["ok"]:
        return {"ok": False, "points": [], "error": out["error"]}
    points = []
    for p in out["predictions"]:
        q = (p.extras or {}).get("quantiles") or {}
        points.append(
            {
                "index": n - len(out["predictions"]) + p.row_index,
                "median": p.label,
                "lo": q.get("0.1"),
                "hi": q.get("0.9"),
                "actual": series[n - len(out["predictions"]) + p.row_index],
            }
        )
    return {"ok": True, "points": points, "error": None}


def cmd_budget_gate(args: argparse.Namespace) -> int:
    """Flag recorded benchmark runs whose actual falls outside the forecast band."""
    table_path = TABLES_DIR / f"bench_{args.name}.csv"
    _, rows = read_table(table_path)
    if len(rows) < 4:
        print(f"Not enough history for {args.name} ({len(rows)} rows, need >= 4) — record more runs first.")
        return EXIT_ERROR
    series = [float(r["duration_s"]) for r in rows]
    telemetry: dict = {}
    out = forecast_band(series, ahead=args.ahead, telemetry=telemetry)
    if not out["ok"]:
        print(f"budget-gate: forecast unavailable — {out['error']}")
        log_record({"op": "budget-gate", "target": args.name, "verdict": "error",
                    "escalated": False, "input_sha256": hashlib.sha256(table_path.read_bytes()).hexdigest()[:16],
                    "heads": {}, "telemetry": telemetry})
        return EXIT_ERROR
    flags = []
    lines = [f"budget-gate: {args.name} — last {len(out['points'])} run(s) vs forecast band"]
    for pt in out["points"]:
        inside = pt["lo"] is not None and pt["hi"] is not None and pt["lo"] <= pt["actual"] <= pt["hi"]
        mark = "ok  " if inside else "FLAG"
        lines.append(
            f"  {mark} run[{pt['index']}]: actual={pt['actual']:.3f}s band=[{pt['lo']:.3f}, {pt['hi']:.3f}] median={pt['median']:.3f}s"
        )
        if not inside:
            flags.append(pt)
    print("\n".join(lines))
    sha = hashlib.sha256(table_path.read_bytes()).hexdigest()[:16]
    log_record(
        {
            "op": "budget-gate",
            "target": args.name,
            "provider": "sdm1",
            "providers_used": ["sdm1"],
            "input_sha256": sha,
            "heads": {"in_band": {"label": "no" if flags else "yes",
                                   "confidence": None}},
            "flags": [{"index": f["index"], "actual": f["actual"], "lo": f["lo"], "hi": f["hi"]} for f in flags],
            "verdict": "escalated" if flags else "pass",
            "escalated": bool(flags),
            "telemetry": telemetry,
        }
    )
    return EXIT_WARN if flags else EXIT_OK


# ── C6: risk-prior — git-history commit risk, cached for the hook ───────────


def directory_features(commits: list[dict]) -> dict[str, dict]:
    """Per-directory features from parsed git log; pure and unit-tested.

    commits: [{dirs: [..], reverted: bool}] — reverted = commit message
    starts with Revert. Features: commits, churn_lines, reverted_touches.
    """
    feats: dict[str, dict] = {}
    for c in commits:
        for d in c.get("dirs") or []:
            f = feats.setdefault(d, {"commits": 0, "churn_lines": 0, "reverted_touches": 0})
            f["commits"] += 1
            f["churn_lines"] += int(c.get("lines", 0) or 0)
            if c.get("reverted"):
                f["reverted_touches"] += 1
    return feats


def parse_git_numstat(log_text: str) -> list[dict]:
    """Parse `git log --numstat --format` output into commit records (pure).

    Splits on "\\n" only — str.splitlines() would eat the \\x1e record
    separator itself (it is in Python's line-separator set).
    """
    commits: list[dict] = []
    current: dict | None = None
    for line in log_text.split("\n"):
        if line.startswith("\x1e"):  # record separator from --format
            if current:
                commits.append(current)
            msg = line[1:].split("\x1f")
            subject = msg[0] if msg else ""
            current = {"subject": subject, "reverted": subject.lower().startswith("revert"), "dirs": [], "lines": 0}
        elif current is not None and "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 3:
                path = parts[2]
                try:
                    add, dele = int(parts[0] or 0), int(parts[1] or 0)
                except ValueError:
                    add = dele = 0
                current["lines"] += add + dele
                d = str(Path(path).parent)
                if d not in current["dirs"]:
                    current["dirs"].append(d)
    if current:
        commits.append(current)
    return commits


def cmd_risk_prior(args: argparse.Namespace) -> int:
    """Score per-directory revert risk from git history into a cached table."""
    repo = Path(args.repo or ".").resolve()
    log_args = ["git", "-C", str(repo), "log", "--numstat", "--format=%x1e%s%x1f%h"]
    if args.since:
        log_args.append(f"--since={args.since}")
    proc = subprocess.run(log_args, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        print(f"error: git log failed — {(proc.stderr or '').strip()[:200]}", file=sys.stderr)
        return EXIT_ERROR
    commits = parse_git_numstat(proc.stdout)
    feats = directory_features(commits)
    if not feats:
        print("No history to score.")
        return EXIT_OK
    context = [
        {
            "dir": d,
            "commits": f["commits"],
            "churn_lines": f["churn_lines"],
            "reverted": "yes" if f["reverted_touches"] else "no",
        }
        for d, f in sorted(feats.items())
    ]
    # context rows carry the revert-history label; query rows are the same
    # directories re-scored with the label withheld — the model smooths
    # revert risk from directory-shape neighbors (commits/churn).
    table = [dict(r) for r in context]
    table += [{**r, "reverted": None} for r in context]
    telemetry: dict = {}
    out = tabular_classify(
        table,
        target="reverted",
        task_type="classification",
        task_id="commit_risk_prior",
        table_name="risk-prior",
        confidence_floor=None,
        telemetry=telemetry,
    )
    rows = []
    if out["ok"]:
        for p in out["predictions"]:
            d = context[p.row_index]["dir"]
            probs = p.probabilities or {}
            # single-class responses are exact complements: P(yes) = 1 - P(no)
            prob = probs.get("yes", (1 - probs["no"]) if "no" in probs else None)
            rows.append({"dir": d, **{k: v for k, v in context[p.row_index].items() if k != "reverted"},
                         "revert_prior": round(prob, 4) if prob is not None else None,
                         "confidence": round(p.confidence, 4) if p.confidence is not None else None})
    else:
        print(f"(sdm1 unavailable: {out['error']} — writing mechanical features only)")
        rows = [{"dir": d, "commits": f["commits"], "churn_lines": f["churn_lines"],
                 "revert_prior": None, "confidence": None} for d, f in sorted(feats.items())]
    table_path = TABLES_DIR / "risk_prior.csv"
    write_table(table_path, ["dir", "commits", "churn_lines", "revert_prior", "confidence"], rows)
    print(f"risk-prior: {len(rows)} directories → {table_path}")
    log_record({"op": "risk-prior", "repo": repo.name, "target": str(table_path),
                "provider": "sdm1" if out["ok"] else "mechanical",
                "input_sha256": hashlib.sha256(proc.stdout.encode()).hexdigest()[:16],
                "heads": {}, "verdict": "pass", "escalated": False, "telemetry": telemetry})
    return EXIT_OK


def load_risk_prior(path: Path | None = None) -> dict[str, float]:
    """Read the cached risk table (hook-path helper: local file, no network)."""
    _, rows = read_table(path or (TABLES_DIR / "risk_prior.csv"))
    return {r["dir"]: float(r["revert_prior"]) for r in rows
            if r.get("revert_prior") not in (None, "", "None")}


def risk_prior_for_paths(priors: dict[str, float], paths: list[str]) -> dict:
    """Max prior over the changed paths' directory ancestry (pure; tested)."""
    best, best_dir = 0.0, None
    for p in paths:
        d = str(Path(p).parent)
        while True:
            if d in priors and priors[d] > best:
                best, best_dir = priors[d], d
            parent = str(Path(d).parent)
            if parent == d:
                break
            d = parent
    return {"prior": round(best, 4) if best_dir else None, "dir": best_dir}


# ── C7: fleet-anomaly — zero-shot repo-metric flags ──────────────────────────


def fleet_metrics(root: Path) -> list[dict]:
    """Per-repo metric rows via git (commits 30d, dirty files, unpushed)."""
    rows = []
    cutoff = time.time() - 30 * 86400
    for repo in sorted(Path(root).iterdir()):
        git = repo / ".git"
        if not git.exists():
            continue
        name = repo.name
        commits_30d = 0
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--since=30 days ago", "--oneline"],
                capture_output=True, text=True, timeout=20, check=True,
            ).stdout
            commits_30d = len([l for l in out.splitlines() if l.strip()])
        except Exception:
            pass
        dirty = 0
        unpushed = 0
        try:
            st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=20, check=True).stdout
            dirty = len([l for l in st.splitlines() if l.strip()])
            unpushed = len([l for l in subprocess.run(
                ["git", "-C", str(repo), "log", "--branches", "--not", "--remotes", "--oneline"],
                capture_output=True, text=True, timeout=20, check=True).stdout.splitlines() if l.strip()])
        except Exception:
            pass
        rows.append({"repo": name, "commits_30d": commits_30d, "dirty_files": dirty,
                     "unpushed_commits": unpushed, "cutoff": int(cutoff)})
    return rows


def cmd_fleet_anomaly(args: argparse.Namespace) -> int:
    """Flag repos whose activity profile deviates from fleet peers."""
    root = Path(args.root or Path.home() / "Projects").expanduser()
    metrics = fleet_metrics(root)
    if len(metrics) < 4:
        print(f"Not enough repos with git history under {root} ({len(metrics)}).")
        return EXIT_ERROR
    # anomaly semantics: target = commits_30d; the observed value rides in
    # commits_30d_actual so query rows stay unlabeled for the model.
    table = []
    for i, m in enumerate(metrics):
        row = dict(m)
        row["commits_30d_actual"] = m["commits_30d"]
        if args.all_query:
            row["commits_30d"] = None
        else:
            row["commits_30d"] = m["commits_30d"]  # context for peers
            if i >= len(metrics) - 1:
                row["commits_30d"] = None
        table.append(row)
    telemetry: dict = {}
    out = tabular_classify(
        table,
        target="commits_30d",
        task_type="anomaly",
        task_id="fleet_anomaly",
        table_name="fleet-anomaly",
        metadata={"actual_column": "commits_30d_actual"},
        confidence_floor=args.floor,
        telemetry=telemetry,
    )
    flags = []
    if out["ok"]:
        for p in out["predictions"]:
            repo = metrics[p.row_index]["repo"]
            if p.label == "anomaly":
                actual = (p.extras or {}).get("actual")
                flags.append({"repo": repo, "actual": actual, "confidence": p.confidence})
                print(f"  FLAG {repo}: commits_30d={actual} outside the fleet band (coverage {p.confidence})")
        quiet = len(metrics) - len(flags)
        print(f"fleet-anomaly: {len(metrics)} repos scored, {len(flags)} flagged, {quiet} in band")
    else:
        print(f"fleet-anomaly: scoring unavailable — {out['error']}")
        print("Mechanical fallback: repos with unpushed commits or dirty trees:")
        for m in metrics:
            if m["unpushed_commits"] or m["dirty_files"]:
                flags.append({"repo": m["repo"], "unpushed": m["unpushed_commits"], "dirty": m["dirty_files"]})
                print(f"  FLAG {m['repo']}: {m['unpushed_commits']} unpushed, {m['dirty_files']} dirty")
    log_record(
        {
            "op": "fleet-anomaly",
            "target": "fleet",
            "provider": "sdm1" if out["ok"] else "mechanical",
            "providers_used": ["sdm1"] if out["ok"] else [],
            "input_sha256": hashlib.sha256(json.dumps(metrics, sort_keys=True).encode()).hexdigest()[:16],
            "heads": {"anomaly": {"label": "yes" if flags else "no",
                                   "confidence": flags[0]["confidence"] if flags and out["ok"] else None}},
            "flags": flags,
            "verdict": "escalated" if flags else "pass",
            "escalated": bool(flags),
            "telemetry": telemetry,
        }
    )
    return EXIT_WARN if flags else EXIT_OK


# ── C8: component routing (eval-only head for triage-issues) ─────────────────


def sdm1_component_features(issue: dict) -> dict:
    """Structured, text-free features for one gh issue (pure; unit-tested)."""
    labels = issue.get("labels") or []
    label_names = [l.get("name", "") if isinstance(l, dict) else str(l) for l in labels]
    known = [n for n in label_names if n and not n.startswith(("bug", "feature", "docs", "chore", "test", "ci"))]
    created = issue.get("createdAt") or ""
    age_days = 0.0
    if created:
        try:
            age_days = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 86400)
        except Exception:
            age_days = 0.0
    return {
        "n_labels": len(label_names),
        "n_component_labels": len(known),
        "component_label": known[0] if known else "none",
        "age_days": round(age_days, 1),
        "has_body": 1 if (issue.get("body") or "").strip() else 0,
        "title_len": len(issue.get("title") or ""),
    }


def route_via_sdm1(issues: list[dict], cfg: dict, telemetry: dict | None = None) -> dict[int, str]:
    """EVAL-ONLY component routing: scores structured issue features via sdm1.

    The result is for calibration comparison only — callers MUST NOT apply
    these as labels (C8 contract; enforced by keeping application code on
    the text-lane path). Returns {issue_number: component_label}.
    """
    if not issues:
        return {}
    context: list[dict] = []
    queries: list[dict] = []
    for issue in issues:
        feats = sdm1_component_features(issue)
        raw_labels = issue.get("labels") or []
        label_names = [l.get("name") if isinstance(l, dict) else str(l) for l in raw_labels]
        component = next(
            (n for n in label_names if n and not n.startswith(("bug", "feature", "docs", "chore", "test", "ci"))),
            None,
        )
        if component:
            context.append({**feats, "component": component})
        else:
            queries.append({**feats, "component": None, "_number": issue.get("number")})
    if len(context) < 3 or not queries:
        if telemetry is not None:
            telemetry.setdefault("sdm1_route", {}).update({"skipped": "thin context or no queries",
                                                            "context": len(context), "queries": len(queries)})
        return {}
    table = context + queries
    out = tabular_classify(
        table,
        target="component",
        task_type="classification",
        task_id="issue_component_route",
        table_name="component-route-eval",
        telemetry=telemetry,
    )
    routed: dict[int, str] = {}
    if out["ok"]:
        for p in out["predictions"]:
            q = queries[p.row_index]
            routed[int(q["_number"])] = str(p.label)
    if telemetry is not None:
        telemetry.setdefault("sdm1_route", {}).update(
            {"routed": len(routed), "context": len(context), "queries": len(queries),
             "eval_only": True}
        )
    return routed
