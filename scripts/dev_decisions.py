#!/usr/bin/env python3
"""
dev-decisions — decision-model gates for git + ZCode workflows.

Subcommands: scan-staged, classify-diff, zcode-gate, install-hooks,
             remove-hooks, log, config, doctor

Stdlib-only (urllib, tomllib, argparse, hashlib, json, os, re, sqlite3, stat,
subprocess, sys, time, datetime). Runs on any python3 ≥ 3.10.

Optional local GLiNER (Phase 2): uv-managed Python 3.12 venv with gliner2[local].
Optional ModernBERT eval (experimental): raw inference for calibration data collection.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ── sys1 bootstrap (optional dependency) ─────────────────────────────────────
# dev-decisions is a single-file stdlib script. When the `sys1` package is
# importable, classify/parse/gate calls delegate to it so the two stay in
# lockstep (one canonical provider implementation, one canonical wire shape).
# When it isn't, the inline providers below keep the tool fully self-contained.
#
# Search order:
#   1. standard Python path (pip install -e / editable / site-packages)
#   2. $DEV_DECISIONS_SYS1_PATH (a directory that contains the `sys1` package)
#   3. ~/Projects/sys1/src (the canonical local checkout)
#   4. ../sys1/src relative to this script (sibling checkout)
#   5. installed sys1 package's parent directory (already in sys.path)
def _bootstrap_sys1():
    try:
        import sys1  # type: ignore[import-not-found]
        return sys1
    except ImportError:
        pass
    candidates: list[Path] = []
    env = os.environ.get("DEV_DECISIONS_SYS1_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "Projects" / "sys1" / "src")
    here = Path(__file__).resolve()
    for parent in here.parents[:4]:
        candidates.append(parent / "sys1" / "src")
    for cand in candidates:
        s = str(cand)
        if not cand.is_dir():
            continue
        if s not in sys.path:
            sys.path.insert(0, s)
        try:
            import sys1  # type: ignore[import-not-found]
            return sys1
        except ImportError:
            # remove and continue — keep sys.path clean on miss
            try:
                sys.path.remove(s)
            except ValueError:
                pass
            continue
    return None


sys1 = _bootstrap_sys1()
try:
    import sys1.plansurface as _plansurface  # type: ignore[import-not-found]  # noqa: E402
except Exception:  # sys1 too old for the plansurface module
    _plansurface = None

# --provider choices: dynamic from the sys1 registry, so candidate models
# (clm, kev, tev1, decide_1b, ...) show up as soon as sys1 registers them —
# plus the fan-out aliases. Falls back to the four core ids when sys1 isn't
# installed (self-contained mode).
_FALLBACK_PROVIDER_IDS = ["decide", "jev", "local", "modernbert"]
_PROVIDER_ALIASES = ["both", "all", "core", "optin", "candidates", "auto"]


def _provider_choices() -> list[str]:
    if sys1 is None:
        return _FALLBACK_PROVIDER_IDS + ["both"]
    try:
        return sorted(sys1.REGISTRY) + _PROVIDER_ALIASES
    except Exception:
        return _FALLBACK_PROVIDER_IDS + ["both"]


PROVIDER_CHOICES = _provider_choices()


def _sys1_chain(provider: str) -> list[str]:
    """Map dev-decisions' provider string to a sys1 provider-chain list.

    The `both` alias here includes local GLiNER because dev-decisions has always
    treated it as part of the `both` fan-out (sys1's own `both` is decide+jev;
    this is the dev-decisions-specific interpretation). Set tokens ("all",
    "core", "optin", "candidates") delegate to sys1.resolve_providers so they
    track the registry; a single id passes through (sys1's flag gate and
    availability checks decide whether it actually runs).
    """
    if provider == "both":
        return ["decide", "jev", "local"]
    if provider in ("all", "core", "optin", "candidates") and sys1 is not None:
        try:
            resolved = sys1.resolve_providers(provider)
            if resolved:
                return resolved
        except Exception:
            pass
    if provider == "all":
        return ["decide", "jev", "local", "modernbert"]
    if "+" in provider:
        return [p for p in provider.split("+") if p]
    return [provider]


# sys1 registers ModernBERT under id "modernbert"; dev-decisions has always
# logged it as "modernbert_raw" so the dashboard can filter it from production
# metrics. The remap keeps both vocabularies intact.
_SYS1_PROVIDER_REMAP = {"modernbert": "modernbert_raw"}


def _sys1_classify(
    provider: str,
    task: str,
    text: str,
    cfg: dict,
) -> dict:
    """
    Delegate a classify call to sys1 and reshape the result into the
    dev-decisions CLI vocabulary:
      - provider IDs remapped (modernbert → modernbert_raw)
      - the same `results / providers_used / telemetry / escalated / summary`
        dict shape the inline code path produces.
    Raises if sys1 isn't importable; callers should fall back to the inline path.
    """
    if sys1 is None:
        raise RuntimeError("sys1 not available")
    chain = _sys1_chain(provider)
    result = sys1.classify(chain, task, text, cfg=cfg, log=False)
    remap = _SYS1_PROVIDER_REMAP
    answers = {remap.get(p, p): a for p, a in result.answers.items()}
    telemetry = {remap.get(p, p): t for p, t in result.telemetry.items()}
    providers_used = [remap.get(p, p) for p in result.providers_used]
    return {
        "results": answers,
        "providers_used": providers_used,
        "telemetry": telemetry,
        "escalated": result.escalated,
        "summary": result.summary,
        "latency_ms": result.latency_ms,
        "verdict": result.verdict,
    }


def _sys1_classify_single(provider: str, task: str, text: str, cfg: dict) -> dict:
    """
    sys1-backed replacement for the per-provider blocks in cmd_pr_gate /
    cmd_triage_issues / cmd_changelog, which expect a single `parsed` dict
    keyed by head id. Returns answers for the first clearing provider, or {}
    if none answered. Falls back to {} so callers can keep their existing
    "extract labels from parsed" loop.
    """
    if sys1 is None:
        raise RuntimeError("sys1 not available")
    # The single-provider commands historically took the base provider (e.g.
    # "decide" or "jev") and used that one — but `both` used decide+jev+local.
    # We preserve the original semantics by running the same chain and using
    # the first provider whose answers are non-empty.
    if provider == "auto":
        resolved_task = sys1.get_task(task) if isinstance(task, str) else task
        chain, _route_reason = sys1.routing.route_decision(resolved_task, text, cfg)
        if not chain:
            chain = _sys1_chain("decide")
    else:
        chain = _sys1_chain(provider)
    result = sys1.classify(chain, task, text, cfg=cfg, log=False)
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        answers = result.answers.get(mapped)
        if answers:
            return {"provider": mapped, "answers": answers, "telemetry": result.telemetry.get(mapped, {})}
    return {"provider": chain[0] if chain else "", "answers": {}, "telemetry": {}}


_FANOUT_MAX_FILES = 12


def _split_diff_files(diff: str, max_files: int = _FANOUT_MAX_FILES) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Split a unified diff into (path, chunk) pairs, largest chunks first, capped
    at max_files. Chunks keep their 'diff --git' header so each section is a
    well-formed per-file diff. Returns (included, omitted_paths).
    """
    chunks: list[tuple[str, str]] = []
    cur_path: str | None = None
    cur: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur_path is not None:
                chunks.append((cur_path, "".join(cur)))
            m = re.search(r" b/(.+)$", line.rstrip("\n"))
            cur_path = m.group(1) if m else line[len("diff --git "):].strip()
            cur = [line]
        elif cur_path is not None:
            cur.append(line)
    if cur_path is not None:
        chunks.append((cur_path, "".join(cur)))
    chunks.sort(key=lambda pc: len(pc[1]), reverse=True)
    return chunks[:max_files], [p for p, _ in chunks[max_files:]]


def _sys1_classify_fanout(provider: str, diff: str, cfg: dict) -> dict:
    """
    Speculative fan-out port (pattern from browser-use/jev-ultrafast): per-file
    heads (risky noul + action choice per file) AND the standard pr_gate heads
    all ride in ONE /v1/systemone request. Questions run in parallel, so
    per-file coverage costs ~nothing in latency over the 3-head call; the app
    uses the heads it needs and discards the rest. Built as a dynamic Task so
    the built-in registry and per-provider head builders are untouched.
    """
    if sys1 is None:
        raise RuntimeError("sys1 not available")
    included, omitted = _split_diff_files(diff)
    heads: list = []
    for h in _build_pr_gate_heads()["jev"]:
        heads.append(sys1.make_choice(h["instructions"], list(h["criteria"].keys()), id=h["id"], descriptions=dict(h["criteria"])))
    file_meta: list[dict] = []
    for i, (path, chunk) in enumerate(included):
        heads.append(sys1.make_noul(
            f"Does the change to {path} touch risky paths (auth, authz, payments, data deletion, secrets, migrations, concurrency)?",
            id=f"f{i}_risky",
        ))
        heads.append(sys1.make_choice(
            f"What should happen for {path} based on its diff?",
            ["approve", "comment", "block"],
            id=f"f{i}_action",
            descriptions={
                "approve": "No action needed for this file.",
                "comment": "Worth a review comment.",
                "block": "Should block the PR until fixed.",
            },
        ))
        file_meta.append({"index": i, "path": path, "chunk": chunk})
    parts = [
        f"PR diff with {len(file_meta) + len(omitted)} changed files. "
        f"Heads f0_risky/f0_action .. f{len(file_meta) - 1}_risky/f{len(file_meta) - 1}_action "
        "map to the numbered file sections below, in order."
    ]
    if omitted:
        parts.append("Files beyond the per-file limit have no heads; they are listed here for context only: " + ", ".join(omitted))
    for m in file_meta:
        parts.append(f"=== FILE f{m['index']}: {m['path']} ===\n{m['chunk']}")
    state = "\n\n".join(parts)[: cfg["scan"]["max_diff_chars"]]
    task = sys1.types.Task(
        id="pr_gate_fanout",
        heads=heads,
        description="PR gate with speculative per-file fan-out heads",
    )
    chain = _sys1_chain(provider) if provider != "auto" else sys1.routing.route_decision(task, state, cfg)[0] or _sys1_chain("decide")
    result = sys1.classify(chain, task, state, cfg=cfg, log=False)
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        answers = result.answers.get(mapped)
        if answers:
            return {
                "provider": mapped,
                "answers": answers,
                "telemetry": result.telemetry.get(mapped, {}),
                "files": [m["path"] for m in file_meta],
                "omitted": omitted,
                "latency_ms": result.latency_ms,
            }
    return {
        "provider": chain[0] if chain else "",
        "answers": {},
        "telemetry": {},
        "files": [m["path"] for m in file_meta],
        "omitted": omitted,
        "latency_ms": result.latency_ms,
    }


def _merge_draws(draws: list) -> dict:
    """
    Majority-vote merge of n per-provider answer dicts (dynamic tasks,
    plan-gate self-consistency). Noul heads: mean p across draws, stdev, and
    an unstable flag when draws straddle the 0.5 cut. Choice/score heads:
    majority label, mean confidence, unstable when labels disagree.
    """
    valid = [d for d in draws if isinstance(d, dict) and d]
    merged: dict = {}
    head_ids: list = []
    for d in valid:
        for hid in d:
            if hid not in head_ids:
                head_ids.append(hid)
    for hid in head_ids:
        vals = [d[hid] for d in valid if isinstance(d.get(hid), dict) and not d[hid].get("_declined")]
        if not vals:
            merged[hid] = {"_declined": "no-answer", "confidence": 0.0}
            continue
        sample = dict(vals[0])
        nouls = [float(v["noul"]) for v in vals if isinstance(v.get("noul"), (int, float))]
        if nouls:
            mean = sum(nouls) / len(nouls)
            stdev = (sum((x - mean) ** 2 for x in nouls) / len(nouls)) ** 0.5 if len(nouls) > 1 else 0.0
            sample["noul"] = round(mean, 4)
            sample["mean"] = round(mean, 4)
            sample["stdev"] = round(stdev, 4)
            sample["unstable"] = len({x >= 0.5 for x in nouls}) > 1
            sample["draws"] = len(nouls)
        else:
            labels = [v.get("label") for v in vals if v.get("label") is not None]
            if labels:
                counts: dict = {}
                for lab in labels:
                    counts[lab] = counts.get(lab, 0) + 1
                sample["label"] = max(counts, key=lambda k: counts[k])
                confs = [float(v["confidence"]) for v in vals if isinstance(v.get("confidence"), (int, float))]
                if confs:
                    sample["confidence"] = round(sum(confs) / len(confs), 4)
                sample["stdev"] = 0.0 if len(counts) == 1 else 1.0
                sample["unstable"] = len(counts) > 1
                sample["draws"] = len(labels)
        merged[hid] = sample
    return merged


def _sys1_consistency(chain: list, task, state: str, cfg: dict, n: int = 3) -> tuple:
    """
    Plan-gate self-consistency: n draws per provider down the chain, first
    provider with any answers wins (same semantics as _sys1_classify_single).
    Returns (merged_answers, provider_used, total_latency_ms, draws_used,
    telemetry) — merged answers carry mean/stdev/unstable per head so the
    2026-10-02 jaggedness finding (3 runs, 3 different flag sets on identical
    input) becomes measurable per run instead of requiring three manual runs.
    """
    used_provider, total_ms, used_draws, used_telemetry = "", 0, 0, {}
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        draws: list = []
        ok = False
        for _ in range(max(1, n)):
            try:
                result = sys1.classify(pid, task, state, cfg=cfg, log=False)
            except Exception:
                break
            total_ms += result.latency_ms
            got = (result.answers or {}).get(mapped) or {}
            if got:
                ok = True
                used_telemetry = result.telemetry.get(mapped, {})
            draws.append(got)
        if ok:
            return _merge_draws(draws), mapped, float(total_ms), sum(1 for d in draws if d), used_telemetry
    return {}, chain[0] if chain else "", float(total_ms), 0, {}


def _diff_paths(diff: str) -> list:
    """Changed paths from diff --git headers (the b/ side)."""
    return [m.group(1) for m in re.finditer(r"^diff --git \S+ b/(.+)$", diff, re.M)]


_DOC_EXTS = (".md", ".rst", ".txt", ".adoc")


def _is_doc_path(path: str) -> bool:
    low = path.lower()
    return low.endswith(_DOC_EXTS) or low.startswith(("docs/", "documentation/"))


def _extract_labels(answers: dict[str, dict], *, prefer_key_substrings: tuple[str, ...] | None = None) -> list[str]:
    """
    Take sys1/dev-decisions answers {head_id: {label/score/noul}} and flatten
    to the set of unique string labels (skipping score/noul values unless they
    are strings). Used by cmd_pr_gate / cmd_triage_issues / cmd_changelog.
    """
    out: list[str] = []
    for head_id, entry in (answers or {}).items():
        if head_id.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        if prefer_key_substrings and not any(s.lower() in head_id.lower() for s in prefer_key_substrings):
            continue
        label = entry.get("label")
        if isinstance(label, list):
            out.extend(str(x) for x in label)
        elif label is not None:
            out.append(str(label))
    return sorted(set(out))


def _legacy_classify(provider: str, task: str, diff: str, cfg: dict) -> dict:
    """
    Inline provider path, preserved verbatim from pre-migration dev-decisions so
    the script still runs standalone when sys1 isn't installed.
    Returns the same shape as _sys1_classify.
    """
    heads = get_task_heads(task, provider.split("+")[0])
    results: dict[str, dict] = {}
    providers_used: list[str] = []
    provider_telemetry: dict[str, dict] = {}
    t0 = time.monotonic()

    if provider in ("decide", "both"):
        decide_key = _env_key("decide")
        if not decide_key:
            print("⚠ FASTINO_API_KEY not set — skipping Decide.", file=sys.stderr)
        else:
            try:
                messages = [
                    {"role": "system", "content": "You are a precise change classifier. Answer only with the requested labels."},
                    {"role": "user", "content": f"Review this git diff and classify it.\n\nDiff:\n{diff}"},
                ]
                pcfg = cfg["providers"]
                decide_telemetry: dict = {}
                resp = _call_openai_compatible(
                    pcfg["decide_api_url"],
                    decide_key,
                    pcfg["decide_model"],
                    messages,
                    schema={"classifications": heads},
                    timeout=pcfg["request_timeout_seconds"],
                    max_retries=pcfg["max_retries"],
                    backoff=pcfg["retry_backoff_seconds"],
                    telemetry=decide_telemetry,
                )
                parsed = _parse_decide_response(resp)
                results["decide"] = parsed
                providers_used.append("decide")
                provider_telemetry["decide"] = decide_telemetry
            except Exception as e:
                print(f"⚠ Decide call failed: {e}", file=sys.stderr)

    if provider in ("jev", "both"):
        jev_key = _env_key("jev")
        if not jev_key:
            print("⚠ TYPESAFE_API_KEY not set — skipping Jev.", file=sys.stderr)
        else:
            try:
                pcfg = cfg["providers"]
                questions = get_task_heads(task, "jev")
                payload_state = {"diff": diff[:DEFAULT_MAX_DIFF_CHARS], "task": task}
                payload_questions: dict[str, dict] = {}
                for q in questions:
                    payload_questions[q["id"]] = {
                        "type": q["type"],
                        "instructions": q["instructions"],
                        "criteria": q.get("criteria"),
                    }
                body = json.dumps({
                    "state": payload_state,
                    "questions": payload_questions,
                    "model": pcfg["jev_model"],
                })
                jev_telemetry: dict = {}
                raw = _call_jev_raw(body, jev_key, cfg, telemetry=jev_telemetry)
                parsed = _parse_jev_response(raw, [q["id"] for q in questions])
                results["jev"] = parsed
                providers_used.append("jev")
                provider_telemetry["jev"] = jev_telemetry
            except Exception as e:
                print(f"⚠ Jev call failed: {e}", file=sys.stderr)

    if provider in ("local", "both"):
        try:
            local_heads = get_task_heads(task, "local")
            local_telemetry: dict = {}
            parsed = _call_local_provider(diff, local_heads, cfg, telemetry=local_telemetry)
            results["local"] = parsed
            providers_used.append("local")
            provider_telemetry["local"] = local_telemetry
        except Exception as e:
            print(f"⚠ Local GLiNER call failed: {e}", file=sys.stderr)

    if provider == "modernbert":
        try:
            modernbert_heads = get_task_heads(task, "modernbert")
            modernbert_telemetry: dict = {}
            parsed = _call_modernbert_provider(diff, modernbert_heads, cfg, telemetry=modernbert_telemetry)
            results["modernbert_raw"] = parsed
            providers_used.append("modernbert_raw")
            provider_telemetry["modernbert_raw"] = modernbert_telemetry
        except Exception as e:
            print(f"⚠ ModernBERT eval failed: {e}", file=sys.stderr)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    escalated = False
    summary: list[str] = []
    for prov, heads_result in results.items():
        summary.append(f"\n── {prov} ──")
        for key, entry in heads_result.items():
            if key.startswith("_"):
                continue
            if not isinstance(entry, dict):
                summary.append(f"  {key}: (declined)")
                escalated = True
                continue
            label = entry.get("label") or entry.get("score") or entry.get("noul") or entry.get("_raw", "?")
            conf = entry.get("confidence")
            label_str = str(label)[:60]
            if conf is not None:
                summary.append(f"  {key}: {label_str} (conf {conf:.2f})")
            else:
                summary.append(f"  {key}: {label_str}")
            if conf is not None and conf < cfg["classify"]["confidence_floor"]:
                escalated = True
            if entry.get("label") is None and entry.get("score") is None and entry.get("noul") is None:
                escalated = True

    return {
        "results": results,
        "providers_used": providers_used,
        "telemetry": provider_telemetry,
        "escalated": escalated,
        "summary": summary,
        "latency_ms": elapsed_ms,
        "verdict": "escalate" if escalated else "pass",
    }


# ── constants ────────────────────────────────────────────────────────────────

VERSION = "0.3.0"
CONFIG_DIR = Path.home() / ".config" / "dev-decisions"
CONFIG_FILE = CONFIG_DIR / "config.toml"
LOG_DIR = Path.home() / ".local" / "share" / "dev-decisions" / "logs"
SURFACES_DIR = Path.home() / ".local" / "share" / "dev-decisions" / "surfaces"
SKILL_DIR = Path.home() / ".agents" / "skills" / "dev-decisions"
HOOKS_DIR = SKILL_DIR / "hooks"
DEFAULT_MAX_DIFF_CHARS = 12_000

# Exit codes (mirrors git hook convention)
EXIT_OK = 0
EXIT_WARN = 1
EXIT_BLOCK = 2
EXIT_ERROR = 3

# ── config ───────────────────────────────────────────────────────────────────

DEFAULTS: dict = {
    "scan": {
        "block_on_secret": True,
        "warn_on_pii": True,
        "max_diff_chars": DEFAULT_MAX_DIFF_CHARS,
    },
    "classify": {
        # "auto" (default) routes through sys1's router: capacity-gated chain,
        # per-task overrides, and the roster default chain (glide,drex,jev)
        # all live in sys1's config, so dev-decisions tracks the roster
        # without code changes here. Explicit ids still pin a provider;
        # without sys1 installed, auto falls back to decide.
        "provider": "auto",          # auto | decide | jev | glide | drex | local | both
        "block_on_classification": False,
        "confidence_floor": 0.7,
        "escalate_on_null": True,
        "allow_vendor_on_sensitive": False,
    },
    "providers": {
        "decide_api_url": "https://api.fastino.ai/v1/chat/completions",
        "decide_model": "fastino/GLiNER-2.5-Decide",
        "jev_api_url": "https://api.typesafe.ai/v1/systemone",
        "jev_model": "jev-1.13.0",
        "local_venv": "/private/tmp/gliner-decide",
        "local_model": "fastino/GLiNER2.5-Decide",
        "modernbert_model": "answerdotai/ModernBERT-base",
        "request_timeout_seconds": 30,
        "max_retries": 2,
        "retry_backoff_seconds": 5,
    },
    "hooks": {
        "pre_commit": "scan-staged",
        "pre_push": "classify-diff",
    },
    "gate": {
        "advisory_only": True,      # True = warn+proceed; False = block
    },
}


def load_config(repo_root: Path | None = None) -> dict:
    """Merge defaults ← global config ← repo .dev-decisions.toml ← env vars."""
    cfg: dict = {}
    # deep-copy defaults
    for section, values in DEFAULTS.items():
        cfg[section] = dict(values)

    # global config
    if CONFIG_FILE.exists():
        try:
            import tomllib
            with open(CONFIG_FILE, "rb") as f:
                user = tomllib.load(f)
            _deep_merge(cfg, user)
        except Exception:
            pass  # never let a broken config file break the tool

    # repo-local override
    if repo_root:
        local = repo_root / ".dev-decisions.toml"
        if local.exists():
            try:
                import tomllib
                with open(local, "rb") as f:
                    local_cfg = tomllib.load(f)
                _deep_merge(cfg, local_cfg)
            except Exception:
                pass

    # env overrides (uppercase, nested with __)
    for key, val in os.environ.items():
        if not key.startswith("DEV_DECISIONS_"):
            continue
        path = key[len("DEV_DECISIONS_"):].lower().split("__")
        _set_nested(cfg, path, _coerce(val))

    # Inherit sys1's provider defaults for keys this config doesn't define.
    # glide/drex and future roster providers live in sys1's DEFAULTS; without
    # this their api_url keys are missing here, so sys1 availability checks
    # mark them unavailable and they can never be routed to.
    if sys1 is not None:
        try:
            for key, val in sys1.load_config().get("providers", {}).items():
                cfg["providers"].setdefault(key, val)
        except Exception:
            pass  # sys1 config unavailable — keep our own defaults
    if cfg["classify"].get("provider") == "auto" and sys1 is None:
        cfg["classify"]["provider"] = "decide"  # no sys1: inline fallback path

    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _set_nested(d: dict, path: list[str], val) -> None:
    for p in path[:-1]:
        d = d.setdefault(p, {})
    d[path[-1]] = val


def _coerce(val: str):
    if val.lower() in ("true", "yes", "1"):
        return True
    if val.lower() in ("false", "no", "0"):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


# ── JSONL logging ────────────────────────────────────────────────────────────

def _log_dir() -> Path:
    return LOG_DIR / datetime.now(timezone.utc).strftime("%Y/%m/%d")


def log_record(record: dict) -> None:
    _log_dir().mkdir(parents=True, exist_ok=True)
    path = _log_dir() / "events.jsonl"
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass  # logging must never break the workflow


# ── git helpers ──────────────────────────────────────────────────────────────

def get_repo_root() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def staged_diff(repo: Path, max_chars: int = DEFAULT_MAX_DIFF_CHARS) -> str:
    """Return the staged diff as text, capped at max_chars."""
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--no-color", "--diff-filter=ACM"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        diff = out.stdout
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n\n... [truncated {len(diff) - max_chars:,} chars]"
        return diff
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def last_commit_diff(repo: Path, max_chars: int = DEFAULT_MAX_DIFF_CHARS) -> str:
    """Return the last commit's diff (HEAD~1..HEAD), capped at max_chars."""
    try:
        out = subprocess.run(
            ["git", "show", "--no-color", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        diff = out.stdout
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n\n... [truncated {len(diff) - max_chars:,} chars]"
        return diff
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def effective_diff(repo: Path, max_chars: int = DEFAULT_MAX_DIFF_CHARS) -> str:
    """Staged diff if present, otherwise the last commit's diff."""
    diff = staged_diff(repo, max_chars)
    if diff.strip():
        return diff
    return last_commit_diff(repo, max_chars)


ZERO_SHA = "0" * 40
# Well-known SHA-1 of git's empty tree; used as the base when a first push
# starts at a root commit. Only SHA-1 repos hit this path in practice.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _is_zero_sha(sha: str) -> bool:
    return bool(sha) and all(c == "0" for c in sha)


def _git_out(repo: Path, argv: list[str]) -> str:
    """Run a git command, returning stdout ('' on any failure)."""
    try:
        out = subprocess.run(
            ["git", *argv], cwd=repo, capture_output=True, text=True, check=True,
        )
        return out.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def read_push_refs() -> list[tuple[str, str]]:
    """Parse git pre-push hook stdin: lines of
    '<local-ref> <local-sha> <remote-ref> <remote-sha>'.
    Returns [(local_sha, remote_sha), ...]. Empty when there is no stdin to read
    (manual run from a TTY, or the ZCode gate which already consumed stdin)."""
    if sys.stdin is None:
        return []
    try:
        if sys.stdin.isatty():
            return []
        data = sys.stdin.read()
    except Exception:
        return []
    refs: list[tuple[str, str]] = []
    for line in data.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        refs.append((local_sha, remote_sha))
    return refs


def _first_push_base(repo: Path, local_sha: str) -> str | None:
    """Base commit for a brand-new branch (remote SHA all zeros): the parent of
    the oldest commit reachable from local_sha but on no remote. Returns None
    when there is nothing new to diff."""
    commits = _git_out(repo, ["rev-list", local_sha, "--not", "--remotes"]).split()
    if not commits:
        return None
    oldest = commits[-1]
    parent = _git_out(repo, ["rev-parse", "--verify", f"{oldest}^"]).strip()
    return parent or EMPTY_TREE


def push_range_diff(repo: Path, refs: list[tuple[str, str]],
                    max_chars: int = DEFAULT_MAX_DIFF_CHARS) -> str:
    """Diff the exact commits being pushed (remote..local per ref), rather than
    just the tip. Returns '' when no usable range is found so the caller can
    fall back to effective_diff()."""
    chunks: list[str] = []
    for local_sha, remote_sha in refs:
        if _is_zero_sha(local_sha):
            continue  # branch/tag deletion — nothing to classify
        if _is_zero_sha(remote_sha):
            base = _first_push_base(repo, local_sha)
            if base is None:
                continue
        else:
            base = remote_sha
        d = _git_out(repo, ["diff", "--no-color", f"{base}..{local_sha}"])
        if d.strip():
            chunks.append(d)
    if not chunks:
        return ""
    text = "\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [truncated {len(text) - max_chars:,} chars]"
    return text


def hook_exit(code: int, args: "argparse.Namespace") -> int:
    """Git hooks have no advisory tier: any non-zero pre-commit/pre-push exit
    aborts the operation. Under a git-hook trigger, downgrade an advisory WARN
    to OK so the human's commit/push proceeds (the warning is still printed).
    The ZCode agent gate and manual runs keep EXIT_WARN so their callers can
    still see the advisory signal. Hard blocks (EXIT_BLOCK) are never softened."""
    trigger = getattr(args, "trigger", "") or ""
    if code == EXIT_WARN and trigger.startswith("git-"):
        return EXIT_OK
    return code


def diff_content_text(diff: str) -> str:
    """Extract content lines from a git diff, stripping git metadata."""
    lines = []
    for line in diff.splitlines():
        # Skip git metadata/plumbing lines
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "@@", "\\ No newline at end of file")):
            continue
        # Skip pure content markers but keep the actual content
        if line.startswith("+") and len(line) > 1:
            lines.append(line[1:])
        elif line.startswith("-") and len(line) > 1:
            lines.append(line[1:])
        elif not line.startswith(("+", "-", " ",)):
            # Context lines (no prefix in unified diff)
            lines.append(line)
    return "\n".join(lines)


def repo_name(repo: Path) -> str:
    return repo.resolve().name


# ── secret/PII scanner ───────────────────────────────────────────────────────

# Patterns from tool-hooks H1 + AWS/JWT/private-key/.env-dump additions.
# Order: most-specific first to reduce false positives.
_SECRET_PATTERNS = [
    # AWS keys (access key ID + secret)
    (re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), "AWS access key ID", "block"),
    # Generic high-entropy secrets (40+ char base64-ish token after common prefixes)
    (re.compile(r"(?:api[_-]?key|apikey|secret|token|password|passwd|private[_-]?key)\s*[=:]\s*['\"]?([A-Za-z0-9_\-]{32,})['\"]?", re.I), "named secret value", "block"),
    # JWT tokens (header.payload.signature base64url)
    (re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"), "JWT token", "block"),
    # PEM private key header
    (re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"), "PEM private key", "block"),
    # .env dump pattern (key=value lines in bulk — catches copied .env contents)
    (re.compile(r"^[A-Z][A-Z0-9_]{2,}=.+$", re.M), "env-var dump", "warn"),
    # GitHub PAT
    (re.compile(r"gh[psu]_[A-Za-z0-9_]{36,}"), "GitHub PAT", "block"),
    # Slack token
    (re.compile(r"xox[baprs]-[0-9a-zA-Z-]+"), "Slack token", "block"),
    # Google API key
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google API key", "block"),
    # Generic "password" in config-ish contexts (looser, warn only)
    (re.compile(r'(?:password|passwd)\s*[=:]\s*\S+', re.I), "password assignment", "warn"),
]

# PII heuristics — warn only, never block (allow --no-verify override).
_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN-like pattern"),
    (re.compile(r"\b\d{16}\b"), "credit-card number (16 digits)"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "email address"),
]


def scan_text(text: str, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Return (secrets, pii_hits). Each hit: {pattern, severity, match, line_context}."""
    secrets: list[dict] = []
    pii: list[dict] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        for pattern, label, severity in _SECRET_PATTERNS:
            m = pattern.search(line)
            if m:
                secrets.append({
                    "pattern": label,
                    "severity": severity,
                    "match": m.group(0)[:80],
                    "line": lineno,
                    "context": line.strip()[:200],
                })
        for pattern, label in _PII_PATTERNS:
            if pattern.search(line):
                pii.append({
                    "pattern": label,
                    "match": m.group(0)[:80] if (m := pattern.search(line)) else "",
                    "line": lineno,
                    "context": line.strip()[:200],
                })
    return secrets, pii


# ── provider clients ─────────────────────────────────────────────────────────

def _env_key(provider: str) -> str | None:
    if provider == "decide":
        return os.environ.get("FASTINO_API_KEY")
    if provider == "jev":
        return os.environ.get("TYPESAFE_API_KEY")
    return None


def _call_openai_compatible(
    api_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    schema: dict | None = None,
    timeout: int = 30,
    max_retries: int = 2,
    backoff: float = 5.0,
    telemetry: dict | None = None,
) -> dict:
    """
    Call an OpenAI-compatible chat-completions endpoint.
    Returns the parsed JSON response body, or raises on final failure.
    If `telemetry` dict is provided, populates it with call-level signals.
    """
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
    }
    if schema is not None:
        body["schema"] = schema

    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_err = ""
    t0 = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                api_url, data=data, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                http_status = resp.status
            parsed = json.loads(raw)
            # surface provider errors
            if "error" in parsed:
                msg = parsed["error"].get("message", str(parsed["error"]))
                if "warming" in msg.lower() and attempt < max_retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise RuntimeError(f"Provider error: {msg}")
            latency_ms = int((time.monotonic() - t0) * 1000)
            if telemetry is not None:
                telemetry["http_status"] = http_status
                telemetry["latency_ms"] = latency_ms
                telemetry["retries"] = attempt
                usage = parsed.get("usage") or {}
                telemetry["token_prompt"] = usage.get("prompt_tokens")
                telemetry["token_completion"] = usage.get("completion_tokens")
                telemetry["token_total"] = usage.get("total_tokens")
                # structured_output_success = we got a parseable body with choices
                telemetry["structured_ok"] = bool(parsed.get("choices"))
            return parsed
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {body_text}"
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
            else:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if telemetry is not None:
                    telemetry["http_status"] = e.code
                    telemetry["latency_ms"] = latency_ms
                    telemetry["retries"] = attempt
                    telemetry["structured_ok"] = False
                raise RuntimeError(last_err) from e
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
            else:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if telemetry is not None:
                    telemetry["latency_ms"] = latency_ms
                    telemetry["retries"] = attempt
                    telemetry["structured_ok"] = False
                raise RuntimeError(f"Request failed: {last_err}") from e

    latency_ms = int((time.monotonic() - t0) * 1000)
    if telemetry is not None:
        telemetry["latency_ms"] = latency_ms
        telemetry["retries"] = max_retries
        telemetry["structured_ok"] = False
    raise RuntimeError(last_err)


def _parse_decide_response(resp: dict) -> dict:
    """Extract {task: {label, confidence}} from Decide response."""
    content = resp["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # fallback: wrap raw content
        return {"_raw": content}


def _parse_jev_response(resp: dict, question_ids: list[str]) -> dict:
    """
    Parse Jev response. The OpenAI-compatible wrapper should return answers
    in one of these shapes:
      a) { "answers": { "<id>": { "type": ..., "choice"/"score"/"noul": ..., "confidence": ... } } }
      b) { "choices": [{"message": {"content": "<json>"}}] }  (JSON string in content)
    Returns {<id>: {label/score/noul, confidence}} keyed by question id.
    """
    # shape a: SDK-style
    if "answers" in resp:
        answers = resp["answers"]
        out: dict = {}
        for qid in question_ids:
            a = answers.get(qid, {})
            entry: dict = {"type": a.get("type", "unknown")}
            if a.get("type") == "choice":
                entry["label"] = a.get("choice")
                entry["confidence"] = a.get("confidence")
                entry["probabilities"] = a.get("probabilities")
            elif a.get("type") == "score":
                entry["score"] = a.get("score")
                entry["legend"] = a.get("legend")
                entry["confidence"] = a.get("confidence")
            elif a.get("type") == "noul":
                entry["noul"] = a.get("noul")
                entry["confidence"] = a.get("confidence")
            out[qid] = entry
        return out

    # shape b: chat-completions content
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "{}")
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "answers" in parsed:
            return _parse_jev_response(parsed, question_ids)
        return parsed
    except (json.JSONDecodeError, KeyError):
        return {"_raw": content}


def _build_local_heads() -> list[dict]:
    """Local GLiNER heads — same semantics as Decide for easy comparison."""
    return [
        {
            "task": "What type of change is this diff? Choose exactly one category.",
            "labels": _DIFF_TYPE_LABELS,
            "multi_label": False,
        },
        {
            "task": "What is the risk level of this change? low = safe, routine; medium = some behavior change; high = affects public APIs, data, auth, or invariants.",
            "labels": _RISK_LABELS,
            "multi_label": False,
        },
    ]


# ── task registry ──────────────────────────────────────────────────────────────
# Each task maps to provider-specific head definitions.
# Heads are built lazily so we don't pay for unused tasks.

def _build_commit_audit_heads() -> dict:
    """Does the commit message accurately describe the diff?"""
    return {
        "decide": [
            {"task": "How accurately does the commit message describe the diff? accurate = fully describes; partial = partially describes; misleading = contradicts or omits major changes.", "labels": ["accurate", "partial", "misleading"], "multi_label": False},
            {"task": "Does the commit message mention every major functional change in the diff?", "labels": ["yes", "no"], "multi_label": False},
        ],
        "jev": [
            {"id": "message_accuracy", "type": "choice", "instructions": "How accurately does the commit message describe the diff? accurate = fully describes; partial = partially describes; misleading = contradicts or omits major changes.", "criteria": {"accurate": "Fully describes the diff.", "partial": "Partially describes the diff.", "misleading": "Contradicts or omits major changes."}},
            {"id": "message_complete", "type": "noul", "instructions": "Does the commit message mention every major functional change in the diff?", "criteria": {"yes": "Yes, all major changes are mentioned.", "no": "No, major changes are missing."}},
        ],
        "local": [
            {"task": "How accurately does the commit message describe the diff? accurate = fully describes; partial = partially describes; misleading = contradicts or omits major changes.", "labels": ["accurate", "partial", "misleading"], "multi_label": False},
        ],
    }


def _build_deps_risk_heads() -> dict:
    """Classify dependency update bump level and risk."""
    return {
        "decide": [
            {"task": "What is the semver bump level of this dependency update? patch = bug fix; minor = new feature, backward-compatible; major = breaking change.", "labels": ["patch", "minor", "major"], "multi_label": False},
            {"task": "What is the risk level? low = patch or minor in low-traffic path; medium = minor in core path or major in low-traffic; high = major in core path or affects auth/data.", "labels": _RISK_LABELS, "multi_label": False},
            {"task": "Does this dependency update introduce a breaking change?", "labels": _BREAKING_LABELS, "multi_label": False},
        ],
        "jev": [
            {"id": "bump_level", "type": "choice", "instructions": "What is the semver bump level? patch = bug fix; minor = new feature, backward-compatible; major = breaking change.", "criteria": {"patch": "Patch-level bug fix.", "minor": "Minor version, new feature, backward-compatible.", "major": "Major version, breaking change."}},
            {"id": "risk_tier", "type": "choice", "instructions": "What is the risk level? low = patch or minor in low-traffic path; medium = minor in core path or major in low-traffic; high = major in core path or affects auth/data.", "criteria": {"low": "Low risk.", "medium": "Medium risk.", "high": "High risk."}},
            {"id": "breaking_change", "type": "noul", "instructions": "Does this dependency update introduce a breaking change?", "criteria": {"yes": "Yes, breaking change.", "no": "No breaking change."}},
        ],
        "local": [
            {"task": "What is the semver bump level of this dependency update? patch = bug fix; minor = new feature, backward-compatible; major = breaking change.", "labels": ["patch", "minor", "major"], "multi_label": False},
            {"task": "What is the risk level? low = patch or minor in low-traffic path; medium = minor in core path or major in low-traffic; high = major in core path or affects auth/data.", "labels": _RISK_LABELS, "multi_label": False},
        ],
    }


def _build_docs_drift_heads() -> dict:
    """Detect docs drift: docs changes without corresponding code changes."""
    return {
        "decide": [
            {"task": "Does this diff include code changes (not just docs)?", "labels": ["code-only", "docs-only", "mixed"], "multi_label": False},
            {"task": "Is there documentation drift — code changed without corresponding doc updates, or docs updated without code changes?", "labels": ["no-drift", "docs-need-update", "code-needs-docs"], "multi_label": False},
        ],
        "jev": [
            {"id": "change_mix", "type": "choice", "instructions": "Does this diff include code changes (not just docs)?", "criteria": {"code-only": "Only code changes.", "docs-only": "Only documentation changes.", "mixed": "Both code and docs changes."}},
            {"id": "docs_drift", "type": "noul", "instructions": "Is there documentation drift — code changed without corresponding doc updates, or docs updated without code changes?", "criteria": {"yes": "Yes, docs drift detected.", "no": "No docs drift."}},
        ],
        "local": [
            {"task": "Does this diff include code changes (not just docs)?", "labels": ["code-only", "docs-only", "mixed"], "multi_label": False},
            {"task": "Is there documentation drift?", "labels": ["no-drift", "drift"], "multi_label": False},
        ],
    }


def _build_api_drift_heads() -> dict:
    """Detect API contract drift / breaking changes."""
    return {
        "decide": [
            {"task": "Does this diff modify any public API surface (routes, schemas, exported functions, types, interfaces, RPC methods)?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "Does this diff introduce a breaking change to the public API?", "labels": _BREAKING_LABELS, "multi_label": False},
            {"task": "If there is a breaking change, how severe is it? high = requires immediate migration; medium = requires migration but has deprecation path; low = backward-compatible.", "labels": ["high", "medium", "low", "none"], "multi_label": False},
        ],
        "jev": [
            {"id": "public_api_modified", "type": "noul", "instructions": "Does this diff modify any public API surface (routes, schemas, exported functions, types, interfaces, RPC methods)?", "criteria": {"yes": "Yes, public API modified.", "no": "No public API modified."}},
            {"id": "breaking_change", "type": "noul", "instructions": "Does this diff introduce a breaking change to the public API?", "criteria": {"yes": "Yes, breaking change.", "no": "No breaking change."}},
            {"id": "severity", "type": "choice", "instructions": "If there is a breaking change, how severe is it?", "criteria": {"high": "High — requires immediate migration.", "medium": "Medium — requires migration but has deprecation path.", "low": "Low — backward-compatible.", "none": "None."}},
        ],
        "local": [
            {"task": "Does this diff modify any public API surface (routes, schemas, exported functions, types, interfaces, RPC methods)?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "Does this diff introduce a breaking change?", "labels": _BREAKING_LABELS, "multi_label": False},
        ],
    }


def _build_pr_gate_heads() -> dict:
    """PR quality gate: change type, risk, suggested labels."""
    _PR_LABELS = ["bug", "feature", "refactor", "docs", "chore", "test", "ci"]
    return {
        "decide": [
            {"task": "What type of change is this PR? Choose the best fit.", "labels": _DIFF_TYPE_LABELS, "multi_label": False},
            {"task": "What is the risk level? low = safe; medium = behavior change; high = public API, data, auth, or invariant change.", "labels": _RISK_LABELS, "multi_label": False},
            {"task": "Which labels should be applied? Pick all that apply.", "labels": _PR_LABELS, "multi_label": True},
        ],
        "jev": [
            {"id": "diff_type", "type": "choice", "instructions": "What type of change is this PR?", "criteria": {l: f"A {l} change." for l in _DIFF_TYPE_LABELS}},
            {"id": "risk_tier", "type": "choice", "instructions": "What is the risk level?", "criteria": {l: f"Risk level {l}." for l in _RISK_LABELS}},
            {"id": "suggested_labels", "type": "choice", "instructions": "Which labels should be applied?", "criteria": {l: f"Apply {l} label." for l in _PR_LABELS}},
        ],
        "local": [
            {"task": "What type of change is this PR?", "labels": _DIFF_TYPE_LABELS, "multi_label": False},
            {"task": "What is the risk level?", "labels": _RISK_LABELS, "multi_label": False},
            {"task": "Which labels should be applied?", "labels": _PR_LABELS, "multi_label": True},
        ],
    }


def _build_issue_triage_heads() -> dict:
    """Issue triage: kind and priority."""
    _ISSUE_KINDS = ["bug", "feature", "docs", "question", "chore"]
    _ISSUE_PRIORITIES = ["low", "medium", "high", "critical"]
    return {
        "decide": [
            {"task": "What kind of issue is this?", "labels": _ISSUE_KINDS, "multi_label": False},
            {"task": "What priority should this issue have?", "labels": _ISSUE_PRIORITIES, "multi_label": False},
        ],
        "jev": [
            {"id": "kind", "type": "choice", "instructions": "What kind of issue is this?", "criteria": {k: f"This is a {k}." for k in _ISSUE_KINDS}},
            {"id": "priority", "type": "choice", "instructions": "What priority should this issue have?", "criteria": {p: f"Priority is {p}." for p in _ISSUE_PRIORITIES}},
        ],
        "local": [
            {"task": "What kind of issue is this?", "labels": _ISSUE_KINDS, "multi_label": False},
            {"task": "What priority should this issue have?", "labels": _ISSUE_PRIORITIES, "multi_label": False},
        ],
    }


def _build_safety_heads() -> dict:
    """Destructive-command safety check."""
    return {
        "decide": [
            {"task": "Does this command perform a destructive operation (force push, rm -rf, DROP/TRUNCATE, destructive migration against production, --no-verify bypass)?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "If destructive, is it reversible?", "labels": ["yes", "no"], "multi_label": False},
        ],
        "jev": [
            {"id": "destructive", "type": "noul", "instructions": "Does this command perform a destructive operation (force push, rm -rf, DROP/TRUNCATE, destructive migration against production, --no-verify bypass)?", "criteria": {"yes": "Yes, this is destructive.", "no": "Not destructive."}},
            {"id": "reversible", "type": "noul", "instructions": "If destructive, is it reversible?", "criteria": {"yes": "Yes, reversible.", "no": "No, irreversible."}},
        ],
        "local": [
            {"task": "Does this command perform a destructive operation?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "If destructive, is it reversible?", "labels": ["yes", "no"], "multi_label": False},
        ],
    }


# task name → builder function
_TASK_BUILDERS: dict[str, Callable[[], dict]] = {
    "change": lambda: {
        "decide": _build_decide_heads(),
        "jev": _build_jev_heads(),
        "local": _build_local_heads(),
        "modernbert": _build_local_heads(),  # eval: same label set, different model
    },
    "commit_audit": lambda: {
        **_build_commit_audit_heads(),
        "modernbert": _build_commit_audit_heads().get("local", []),
    },
    "deps_risk": lambda: {
        **_build_deps_risk_heads(),
        "modernbert": _build_deps_risk_heads().get("local", []),
    },
    "docs_drift": lambda: {
        **_build_docs_drift_heads(),
        "modernbert": _build_docs_drift_heads().get("local", []),
    },
    "api_drift": lambda: {
        **_build_api_drift_heads(),
        "modernbert": _build_api_drift_heads().get("local", []),
    },
    "pr_gate": lambda: {
        **_build_pr_gate_heads(),
        "modernbert": _build_pr_gate_heads().get("local", []),
    },
    "issue_triage": lambda: {
        **_build_issue_triage_heads(),
        "modernbert": _build_issue_triage_heads().get("local", []),
    },
    "safety": _build_safety_heads,  # no modernbert: regex-driven, no training data yet
}


def get_task_heads(task: str, provider: str) -> list[dict]:
    """Return provider-specific heads for a named task."""
    task_def = _TASK_BUILDERS.get(task, _TASK_BUILDERS["change"])()
    return task_def.get(provider, task_def.get("decide", []))


def detect_task_from_diff(diff: str, paths: list | None = None) -> str:
    paths = paths if paths is not None else _diff_paths(diff)
    diff_lower = diff.lower()
    dep_patterns = ["package.json", "package-lock.json", "yarn.lock", "requirements.txt",
                    "pyproject.toml", "Pipfile", "poetry.lock", "pom.xml", "build.gradle",
                    "Cargo.toml", "go.mod", "composer.json", "Gemfile"]
    # dep manifests are matched on CHANGED PATHS (a README mentioning
    # package.json used to route here via body text)
    if any(any(d in pp.lower() for d in dep_patterns) for pp in paths):
        return "deps_risk"
    # docs-only = every changed path is a doc file (was: any ".md" anywhere in
    # the diff body, which code diffs mentioning a markdown path also hit)
    if paths and all(_is_doc_path(pp) for pp in paths):
        return "docs_drift"
    return "change"

def _call_jev_raw(body: str, api_key: str | None, cfg: dict, telemetry: dict | None = None) -> dict:
    """
    Call Jev /v1/systemone and return the parsed JSON response dict.
    Retries on transient errors. Raises RuntimeError on failure.
    If `telemetry` dict is provided, populates it with HTTP-level signals.
    """
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY not set")

    url = cfg["providers"]["jev_api_url"]
    timeout = cfg["providers"]["request_timeout_seconds"]
    max_retries = cfg["providers"]["max_retries"]
    backoff = cfg["providers"]["retry_backoff_seconds"]

    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body.encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                http_status = resp.status
            latency_ms = int((time.monotonic() - t0) * 1000)
            if telemetry is not None:
                telemetry["http_status"] = http_status
                telemetry["latency_ms"] = latency_ms
                telemetry["retries"] = attempt
            try:
                return json.loads(raw_bytes.decode())
            except json.JSONDecodeError:
                if telemetry is not None:
                    telemetry["structured_ok"] = False
                raise RuntimeError(f"Jev returned non-JSON: {raw_bytes[:200]}")
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode()[:300]}"
            if telemetry is not None:
                telemetry["http_status"] = e.code
                telemetry["latency_ms"] = int((time.monotonic() - t0) * 1000)
                telemetry["retries"] = attempt
                telemetry["structured_ok"] = False
            if e.code >= 500 and attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError(f"Jev HTTP error: {last_err}")
        except Exception as e:
            last_err = str(e)
            if telemetry is not None:
                telemetry["latency_ms"] = int((time.monotonic() - t0) * 1000)
                telemetry["retries"] = attempt
                telemetry["structured_ok"] = False
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError(f"Jev call failed: {last_err}")
    if telemetry is not None:
        telemetry["latency_ms"] = int((time.monotonic() - t0) * 1000)
        telemetry["retries"] = max_retries
        telemetry["structured_ok"] = False
    raise RuntimeError(f"Jev exhausted retries: {last_err}")


def _call_modernbert_provider(text: str, heads: list[dict], cfg: dict, telemetry: dict | None = None) -> dict:
    """
    Experimental ModernBERT eval provider (no fine-tuning).

    Uses ModernBERT-base as a sentence encoder: encodes the input text once,
    then scores each candidate label by cosine similarity against the label's
    standalone embedding. Returns {task_text: {label, confidence, all_scores}}
    or raises. all_scores is kept for calibration dashboards.

    Results are logged with provider tag 'modernbert_raw' so they can be
    filtered from production metrics and used to understand what fine-tuning
    would need to improve.
    """
    import math

    venv_python = Path(cfg["providers"].get("local_venv", "/private/tmp/gliner-decide")) / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"Local venv not found at {venv_python}")

    model_name = cfg["providers"].get("modernbert_model", "answerdotai/ModernBERT-base")
    max_chars = cfg["scan"]["max_diff_chars"]
    text_truncated = text[:max_chars]

    # Build inline script that runs inside the venv
    inline = f'''
import json, sys, math
import numpy as np
from transformers import AutoTokenizer, AutoModel

model_name = {model_name!r}
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModel.from_pretrained(model_name)
model.eval()

def to_python(val):
    """Convert numpy types to native Python for JSON serialization."""
    if hasattr(val, "item"):
        return val.item()
    return val

def cosine(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

def encode(text):
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=8192, padding=False)
    with torch.no_grad():
        outputs = model(**inputs)
    # [CLS] token embedding (index 0)
    cls = outputs.last_hidden_state[0, 0, :].numpy()
    # Normalize
    norm = np.linalg.norm(cls)
    if norm > 0:
        cls = cls / norm
    return cls

try:
    text_emb = encode(sys.argv[1])
    heads = json.loads(sys.argv[2])
    results = {{}}
    for tid, task in heads.items():
        labels = task.get("labels", [])
        if not labels:
            results[tid] = {{"label": None, "confidence": 0.0}}
            continue
        # Score each label by similarity to its standalone embedding
        scores = []
        for label in labels:
            label_emb = encode(label)
            scores.append(cosine(text_emb, label_emb))
        best_idx = max(range(len(scores)), key=lambda i: scores[i])
        best_label = labels[best_idx]
        best_score = scores[best_idx]
        # Compute top2 gap for calibration
        sorted_scores = sorted(scores, reverse=True)
        top2_gap = float(sorted_scores[0] - sorted_scores[1]) if len(sorted_scores) > 1 else 1.0
        results[tid] = {{
            "label": to_python(best_label),
            "confidence": to_python(round(best_score, 4)),
            "all_scores": {{to_python(l): to_python(round(s, 4)) for l, s in zip(labels, scores)}},
            "top2_gap": to_python(round(top2_gap, 4)),
            "embedding_norm": to_python(round(float(np.linalg.norm(text_emb)), 4)),
        }}
    print(json.dumps(results))
except Exception as e:
    print(json.dumps({{"_error": str(e)}}))
'''

    # Need torch in the inline script too
    inline = "import torch\n" + inline

    tasks_json = json.dumps({h["task"][:40]: {"labels": h.get("labels", [])} for h in heads})

    t0 = time.monotonic()
    proc = subprocess.run(
        [str(venv_python), "-c", inline, text_truncated, tasks_json],
        capture_output=True,
        text=True,
        timeout=cfg["providers"]["request_timeout_seconds"] * 2,  # ModernBERT is slower on CPU
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    if telemetry is not None:
        telemetry["latency_ms"] = latency_ms
        if proc.returncode != 0:
            stderr = (proc.stderr or "")[:300]
            telemetry["error_kind"] = "timeout" if "timeout" in stderr.lower() else "model_load" if "model" in stderr.lower() else "other"
            telemetry["stderr"] = stderr

    if proc.returncode != 0:
        raise RuntimeError(f"ModernBERT eval failed: {proc.stderr[:300]}")

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        parsed = {"_raw": proc.stdout[:200]}
        if telemetry is not None:
            telemetry["error_kind"] = "json"

    # Normalize output shape to match other providers
    out: dict = {}
    for tid, entry in parsed.items():
        if isinstance(entry, dict) and "_error" not in entry:
            # Cast numpy types to native Python for JSON safety
            label = entry.get("label")
            conf = entry.get("confidence")
            if hasattr(conf, "item"):  # numpy scalar
                conf = conf.item()
            if hasattr(label, "item"):
                label = label.item()
            out[tid] = {
                "label": label,
                "confidence": conf,
                # keep all_scores, top2_gap, embedding_norm for dashboards
                **{k: v for k, v in entry.items() if k in ("all_scores", "top2_gap", "embedding_norm")},
            }
        else:
            out[tid] = {"_raw": str(entry)}
    return out


def _call_local_provider(diff: str, heads: list[dict], cfg: dict, telemetry: dict | None = None) -> dict:
    """
    Call local GLiNER2 via the existing /private/tmp/gliner-decide venv.
    Returns {task_text: {label, confidence}} or raises on failure.
    If `telemetry` dict is provided, populates it with subprocess signals.
    """
    venv_python = Path(cfg["providers"].get("local_venv", "/private/tmp/gliner-decide")) / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"Local GLiNER venv not found at {venv_python}")

    # Build the inline script that runs inside the venv
    tasks_json = json.dumps({h["task"][:40]: {"labels": h.get("labels", []), "instruction": h["task"]} for h in heads})
    inline = f'''
import json, sys, io

# Suppress model init banner
old_stdout = sys.stdout
sys.stdout = io.StringIO()

from gliner2.classification import ClassificationConfig, ClassificationSchema, Classifier

model_name = {cfg["providers"]["local_model"]!r}
text = sys.argv[1]
tasks = json.loads(sys.argv[2])

# Fastino-prescribed usage (gliner25-decide-playground/model.py): one
# ClassificationSchema holding every task, one decode, independent decoder —
# the same surface sys1's local wire uses, so the two agree. instruction
# carries the head's question text; without it the model only sees labels.
schema = ClassificationSchema()
for name, spec in tasks.items():
    schema.single(name, spec["labels"], instruction=spec.get("instruction"))
clf = Classifier.from_pretrained(model_name, map_location="cpu")
result = clf.classify(text, schema, config=ClassificationConfig(decoder="independent", on_infeasible="relax"))

out = {{}}
for name in tasks:
    try:
        value = result.value(name)
        probs = result.probabilities(name)
    except Exception:
        continue  # task absent from the result -> missing below
    labels = list(value) if isinstance(value, (list, tuple)) else [value]
    out[name] = {{
        "label": str(labels[0]) if labels else None,
        "confidence": max(probs.values()) if probs else None,
    }}

# Restore stdout and print only the JSON result
sys.stdout = old_stdout
print(json.dumps(out))
'''

    t0 = time.monotonic()
    proc = subprocess.run(
        [str(venv_python), "-c", inline, diff[: cfg["scan"]["max_diff_chars"]], tasks_json],
        capture_output=True, text=True, timeout=cfg["providers"]["request_timeout_seconds"],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    if telemetry is not None:
        telemetry["latency_ms"] = latency_ms
        if proc.returncode != 0:
            telemetry["error_kind"] = "other"
            telemetry["stderr"] = (proc.stderr or "")[:300]

    if proc.returncode != 0:
        raise RuntimeError(f"Local GLiNER failed: {proc.stderr[:300]}")

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        parsed = {"_raw": proc.stdout[:200]}
        if telemetry is not None:
            telemetry["error_kind"] = "json"

    # Normalize: keys are task[:40] from caller
    out: dict = {}
    null_count = 0
    for tid, entry in parsed.items():
        if isinstance(entry, dict):
            label = entry.get("label")
            if label is None:
                null_count += 1
            out[tid] = {
                "label": label,
                "confidence": entry.get("confidence"),
            }
        else:
            out[tid] = {"_raw": str(entry)}
    if telemetry is not None:
        telemetry["null_label_count"] = null_count
    return out


def _call_local_pii_scan(diff: str, cfg: dict) -> dict:
    """
    Run local GLiNER2 PII span extraction via the existing venv.
    Takes a raw git diff, strips git metadata, and scans content text.
    Returns {entity_type: [text, ...]} or raises on failure.
    """
    venv_python = Path(cfg["providers"].get("local_venv", "/private/tmp/gliner-decide")) / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"Local GLiNER venv not found at {venv_python}")

    pii_model = cfg["providers"].get("pii_model", "fastino/gliner2-privacy-filter-PII-multi")
    pii_labels = cfg.get("scan", {}).get("pii_labels", ["person", "email", "phone", "ssn", "address"])

    # Strip git diff metadata — only scan actual content
    content = diff_content_text(diff)
    if not content.strip():
        return {"entities": {}}

    inline = f'''
import json, sys, io, contextlib
from gliner2 import GLiNER2

# Suppress model init banner
old_stdout = sys.stdout
sys.stdout = io.StringIO()

model = GLiNER2.from_pretrained({pii_model!r})
text = sys.argv[1]
labels = json.loads(sys.argv[2])
schema = model.create_schema().entities(labels).build()
result = model.extract(text, schema)

# Restore stdout and print only the JSON result
sys.stdout = old_stdout
print(json.dumps(result))
'''

    proc = subprocess.run(
        [str(venv_python), "-c", inline, content[: cfg["scan"]["max_diff_chars"]], json.dumps(pii_labels)],
        capture_output=True, text=True, timeout=cfg["providers"]["request_timeout_seconds"],
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Local PII scan failed: {proc.stderr[:300]}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_raw": proc.stdout[:200]}


# ── classify-diff heads ──────────────────────────────────────────────────────

_DIFF_TYPE_LABELS = ["feat", "fix", "refactor", "docs", "test", "chore", "deps", "style"]
_RISK_LABELS = ["low", "medium", "high"]
_BREAKING_LABELS = ["yes", "no"]
_PII_IN_DIFF_LABELS = ["yes", "no"]


def _build_decide_heads() -> list[dict]:
    return [
        {
            "task": "What type of change is this diff? Choose exactly one category.",
            "labels": _DIFF_TYPE_LABELS,
            "multi_label": False,
        },
        {
            "task": "What is the risk level of this change? low = safe, routine; medium = some behavior change; high = affects public APIs, data, auth, or invariants.",
            "labels": _RISK_LABELS,
            "multi_label": False,
        },
    ]


def _build_jev_heads() -> list[dict]:
    # Jev-style Choice questions with criteria descriptions (first-class).
    return [
        {
            "id": "diff_type",
            "type": "choice",
            "instructions": "What type of change is this diff? Choose exactly one category.",
            "criteria": {l: f"A {l} change." for l in _DIFF_TYPE_LABELS},
        },
        {
            "id": "risk_tier",
            "type": "choice",
            "instructions": "What is the risk level? low = safe routine; medium = some behavior change; high = public API, data, auth, or invariant change.",
            "criteria": {l: f"Risk level {l}." for l in _RISK_LABELS},
        },
        {
            "id": "breaking_change",
            "type": "noul",
            "instructions": "Does this diff introduce a breaking change — removes a public API, changes a response schema, or requires migration?",
            "criteria": {
                "yes": "Yes, this is a breaking change.",
                "no": "No breaking change.",
            },
        },
        {
            "id": "personal_data",
            "type": "noul",
            "instructions": "Does the diff add or modify code that handles personal data (PII, student records, health, financial)?",
            "criteria": {
                "yes": "Yes, personal data is involved.",
                "no": "No personal data.",
            },
        },
    ]


# ── subcommands ──────────────────────────────────────────────────────────────

def cmd_scan_staged(args: argparse.Namespace) -> int:
    repo = get_repo_root()
    if not repo:
        print("error: not in a git repository", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(repo)
    diff = staged_diff(repo, cfg["scan"]["max_diff_chars"])
    if not diff:
        print("No staged changes to scan.")
        return EXIT_OK

    secrets, pii = scan_text(diff, cfg)

    # Deep PII scan with local GLiNER span model (fully local — works on sensitive repos)
    pii_spans: list[dict] = []
    if getattr(args, "deep", False):
        try:
            pii_result = _call_local_pii_scan(diff, cfg)
            entities = pii_result.get("entities", {})
            for entity_type, texts in entities.items():
                for text in texts:
                    pii_spans.append({
                        "type": entity_type,
                        "text": text,
                        "source": "gliner2-pii-model",
                    })
        except Exception as e:
            print(f"⚠ Deep PII scan failed: {e}", file=sys.stderr)

    # Merge regex PII + model PII spans for logging/output
    all_pii = pii + pii_spans

    # Log
    log_record({
        "op": "scan-staged",
        "repo": repo_name(repo),
        "trigger": args.trigger or "manual",
        "input_chars": len(diff),
        "input_sha256": hashlib.sha256(diff.encode()).hexdigest()[:16],
        "secrets_found": len(secrets),
        "pii_found": len(all_pii),
        "pii_spans": len(pii_spans),
        "verdict": "block" if secrets else ("warn" if all_pii else "pass"),
    })

    if secrets:
        block = [s for s in secrets if s["severity"] == "block"]
        if block and cfg["scan"]["block_on_secret"]:
            print(f"\033[1;31mBLOCKED — {len(block)} secret(s) detected:\033[0m", file=sys.stderr)
            for s in block:
                print(f"  line {s['line']}: [{s['pattern']}] {s['match']}", file=sys.stderr)
                print(f"    {s['context']}", file=sys.stderr)
            print("\nRemove the secret or run `git commit --no-verify` to bypass.", file=sys.stderr)
            return EXIT_BLOCK
        else:
            print(f"\033[1;33mWARNING — {len(secrets)} potential secret(s):\033[0m")
            for s in secrets:
                print(f"  line {s['line']}: [{s['pattern']}] {s['match']}")

    if all_pii and cfg["scan"]["warn_on_pii"]:
        print(f"\033[1;33mPII hint — {len(all_pii)} potential PII hit(s):\033[0m")
        # Show regex hits first
        for p in pii[:5]:
            print(f"  line {p['line']}: [{p['pattern']}] {p['match']}")
        # Then model spans
        for span in pii_spans[:5]:
            print(f"  [model] {span['type']}: {span['text']}")
        if len(all_pii) > 5:
            print(f"  ... and {len(all_pii) - 5} more")

    if not secrets and not all_pii:
        print("✓ No secrets or PII detected in staged changes.")
        return EXIT_OK

    return hook_exit(EXIT_WARN if (secrets and not block) else EXIT_OK, args)


def cmd_classify_diff(args: argparse.Namespace) -> int:
    repo = get_repo_root()
    if not repo:
        print("error: not in a git repository", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(repo)

    # A pre-push hook hands us the exact commits being pushed on stdin
    # (ref-lines). Manual and agent-gate runs have no stdin, so fall back to
    # the staged-or-tip diff. Diff the pushed range, not just the tip commit.
    trigger = getattr(args, "trigger", "") or ""
    refs = read_push_refs() if trigger.startswith("git-") else []
    diff = push_range_diff(repo, refs, cfg["scan"]["max_diff_chars"]) if refs else ""
    if not diff:
        diff = effective_diff(repo, cfg["scan"]["max_diff_chars"])
    if not diff:
        print("Nothing to classify.")
        return EXIT_OK

    # vendor guard: run local scan first
    secrets, pii = scan_text(diff, cfg)
    if secrets:
        blocking = [s for s in secrets if s["severity"] == "block"]
        hard_block = bool(blocking) and cfg["scan"]["block_on_secret"]
        print("⚠ Secret-shaped content detected — skipping vendor classification (policy).")
        for s in secrets[:8]:
            tag = "BLOCK" if s["severity"] == "block" else "advisory"
            print(f"    [{tag}] {s['pattern']}: {s['match']}  (line {s['line']})")
        # A real secret must never reach a hosted provider, and it aborts.
        # Identifier-shape matches (e.g. `password: z.string()` in a schema,
        # placeholder env values) are advisory only — under a git hook they must
        # not block the human's push, since git treats ANY non-zero pre-push
        # exit as a failed push.
        return hook_exit(EXIT_BLOCK if hard_block else EXIT_WARN, args)

    # repo-level sensitive flag
    local_cfg_path = repo / ".dev-decisions.toml"
    sensitive = False
    if local_cfg_path.exists():
        try:
            import tomllib
            with open(local_cfg_path, "rb") as f:
                sensitive = tomllib.load(f).get("repo", {}).get("sensitive", False)
        except Exception:
            pass

    if sensitive and not cfg["classify"]["allow_vendor_on_sensitive"]:
        if args.allow_vendor:
            pass  # explicit opt-in
        else:
            print("⚠ Repo is marked sensitive — vendor classification skipped.")
            print("   Pass --allow-vendor to override.")
            return EXIT_WARN

    provider = args.provider or cfg["classify"]["provider"]
    changed_paths = _diff_paths(diff)
    task = args.task or detect_task_from_diff(diff, changed_paths)

    # ── classify: sys1 library if importable, inline providers otherwise ──
    if sys1 is not None:
        outcome = _sys1_classify(provider, task, diff, cfg)
    else:
        outcome = _legacy_classify(provider, task, diff, cfg)

    results: dict[str, dict] = outcome["results"]
    providers_used: list[str] = outcome["providers_used"]
    provider_telemetry: dict[str, dict] = outcome["telemetry"]
    escalated: bool = outcome["escalated"]
    gate_summary: list[str] = outcome["summary"]
    elapsed_ms: int = outcome["latency_ms"]

    if not results:
        print("No provider succeeded — diff unclassified.")
        return EXIT_BLOCK if cfg["classify"]["block_on_classification"] else EXIT_OK

    # Mechanical pushdown (2026-10-02 graded evidence): a diff whose changed
    # paths are all doc files cannot introduce code drift. Force the drift
    # noul to no and change_mix to docs-only in code — the model is not asked
    # what code already decides. Same fail-closed-override precedent as
    # evidence-gate's verdict downgrade.
    overridden: list = []
    if task == "docs_drift" and changed_paths and all(_is_doc_path(pp) for pp in changed_paths):
        for prov, heads in results.items():
            if not isinstance(heads, dict):
                continue
            for hid, a in list(heads.items()):
                if not isinstance(a, dict):
                    continue
                if "drift" in hid.lower() and isinstance(a.get("noul"), (int, float)):
                    if a["noul"] >= 0.5:
                        a["noul"] = 0.0
                        overridden.append(f"{prov}:{hid}")
                if "change_mix" in hid.lower() and a.get("label") not in (None, "docs-only"):
                    a["label"] = "docs-only"
                    overridden.append(f"{prov}:{hid}")
        if overridden:
            print("  [pushdown] all-docs diff: drift forced to no (code rule, not model).")

    print("\n".join(gate_summary))
    if escalated:
        print(f"\n⚠ Low confidence or null verdict — human review recommended (floor {cfg['classify']['confidence_floor']}).")

    # Log
    log_record({
        "op": "classify-diff",
        "repo": repo_name(repo),
        "trigger": args.trigger or "manual",
        "task": task,
        "provider": provider,
        "providers_used": providers_used,
        "input_chars": len(diff),
        "input_sha256": hashlib.sha256(diff.encode()).hexdigest()[:16],
        "heads": _sanitize_for_log(results),
        "overridden": overridden,
        "verdict": "escalated" if escalated else "pass",
        "escalated": escalated,
        "latency_ms": elapsed_ms,
        "telemetry": provider_telemetry,
    })

    if escalated and cfg["classify"]["block_on_classification"]:
        return EXIT_BLOCK
    return hook_exit(EXIT_OK if not escalated else EXIT_WARN, args)


def _sanitize_for_log(results: dict) -> dict:
    """Strip anything that looks like a secret from logged head values."""
    import copy
    safe = copy.deepcopy(results)
    for prov, heads in safe.items():
        if not isinstance(heads, dict):
            continue
        for k, v in heads.items():
            if isinstance(v, dict):
                for field in ("content", "context", "match", "_raw"):
                    if field in v and isinstance(v[field], str):
                        v[field] = "[redacted]"
    return safe


_DESTRUCTIVE_PATTERNS = [
    r"git\s+push\s+.*--force",
    r"rm\s+-rf\s+",
    r"DROP\s+TABLE",
    r"TRUNCATE\s+TABLE",
    r"alembic\s+.*(?:upgrade|downgrade).*head",
    r"migrate\s+.*(?:up|down).*production",
    r"rails\s+db:migrate",
    r"kubectl\s+delete",
]


def _strip_inert_spans(cmd: str) -> str:
    """
    Remove text that cannot execute: single/double-quoted spans and heredoc
    bodies. Command substitutions ($( ... ), `...`) deliberately STAY in the
    skeleton because they run. The destructive-pattern gate matches against
    this skeleton, so a grep ARGUMENT or a commit MESSAGE quoting dangerous
    words stops tripping the gate (8 graded false positives 2026-10-02, two
    at block severity).
    """
    out = []
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch in ("'", '"'):
            # Exception: a quoted span is NOT inert when it is the argument of
            # an interpreter flag — `psql -c 'DROP TABLE users'` executes the
            # SQL. Keep such spans in the skeleton.
            prev_token = re.search(r"(\S+)\s*$", "".join(out))
            prev = prev_token.group(1) if prev_token else ""
            if prev == "eval" or prev == "exec" or prev.endswith("-c"):
                end = cmd.find(ch, i + 1)
                span_end = (end + 1) if end != -1 else n
                out.append(cmd[i:span_end])
                i = span_end
                continue
            quote = ch
            i += 1
            while i < n:
                if quote == '"' and cmd[i] == chr(92):
                    i += 2
                    continue
                if cmd[i] == quote:
                    if i + 1 < n and cmd[i + 1] == quote:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            out.append(" ")
        elif cmd.startswith("<<", i):
            m = re.match(r"<<-?\s*(['\"]?)(\w+)\1", cmd[i:])
            if m:
                tag = m.group(2)
                line_end = cmd.find("\n", i)
                if line_end == -1:
                    i = n
                    out.append(" ")
                    continue
                body_start = line_end + 1
                m_end = re.search(rf"^{re.escape(tag)}\s*$", cmd[body_start:], re.M)
                i = body_start + m_end.end() if m_end else n
                out.append(" ")
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cmd_zcode_gate(args: argparse.Namespace) -> int:
    """
    ZCode PreToolUse hook: reads JSON from stdin with keys:
      { tool_name, command, tool_input, cwd, ... }

    Fast-exit on non-git-commit commands. On git commit/push, run scan-staged.
    Also detects destructive patterns and runs safety task.
    Exit 2 blocks the agent's command.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        payload = {}

    tool = payload.get("tool_name", "")
    command = payload.get("tool_input", {}).get("command", "") if isinstance(payload.get("tool_input"), dict) else ""

    # Only gate Bash commands
    if tool != "Bash":
        return EXIT_OK
    git_cmd = command.strip()
    if not git_cmd:
        return EXIT_OK

    cfg = load_config(None)
    gate_cfg = cfg.get("gate", {})
    advisory_only = gate_cfg.get("advisory_only", True)

    # ── destructive-pattern detection ─────────────────────────────────────
    # Patterns match the INERT-STRIPPED skeleton: quoted spans and heredoc
    # bodies are text, not commands. --no-verify is a hook bypass, not a
    # destructive op (graded false positive 2026-10-02), so it surfaces as a
    # separate advisory flag instead of feeding the block path.
    skeleton = _strip_inert_spans(git_cmd)
    hook_bypass = bool(re.search(r"--no-verify", skeleton, re.IGNORECASE))
    destructive_hits = [p for p in _DESTRUCTIVE_PATTERNS if re.search(p, skeleton, re.IGNORECASE)]

    if destructive_hits:
        print(f"[gate] destructive pattern detected in: {git_cmd}", file=sys.stderr)
        print(f"[gate] patterns: {', '.join(destructive_hits)}", file=sys.stderr)

        # Regex match is authoritative for destructive flag; model refines reversibility
        destructive = True
        reversible = "unknown"
        reversible_conf = None

        # Run the safety task through sys1's router (hosted roster; local
        # providers are out of the pool). Without sys1, reversibility stays
        # unknown and the high-stakes regex rule decides.
        try:
            safety_telemetry: dict = {}
            safety_result: dict = {}
            reversible_p: float | None = None
            if sys1 is not None:
                single = _sys1_classify_single("auto", "safety", git_cmd, cfg)
                safety_result = single.get("answers", {})
                safety_telemetry = single.get("telemetry", {})
            for key, val in safety_result.items():
                if isinstance(val, dict) and "reversible" in key.lower():
                    if isinstance(val.get("noul"), (int, float)):
                        reversible_p = float(val["noul"])
                        reversible = f"P(reversible)={reversible_p:.2f}"
                    else:
                        label = val.get("label")
                        if label:
                            reversible = str(label)
                            reversible_conf = val.get("confidence")
        except Exception as e:
            print(f"[gate] safety task failed: {e} — reversibility unknown", file=sys.stderr)

        print(f"[gate] safety task: DESTRUCTIVE (reversible={reversible})", file=sys.stderr)
        reason = f"Destructive operation detected (reversible={reversible})."

        # HITL policy (2026-10-02): destructive AND more-likely-irreversible
        # hard-blocks regardless of advisory_only — the only verdict with no
        # favorable cost asymmetry to trade. When the safety model cannot
        # refine reversibility, the high-stakes patterns still hard-block
        # (presumed irreversible); lower-stakes destructive hits stay advisory.
        _high_stakes = any(p in destructive_hits for p in (
            _DESTRUCTIVE_PATTERNS[0], _DESTRUCTIVE_PATTERNS[2], _DESTRUCTIVE_PATTERNS[3],
            _DESTRUCTIVE_PATTERNS[7]))
        irreversible = (reversible_p is not None and reversible_p < 0.5) or (
            reversible_p is None and str(reversible).lower() in ("no", "false"))
        if irreversible or (_high_stakes and reversible_p is None and reversible == "unknown"):
            why = "destructive AND irreversible" if irreversible else "high-stakes destructive, reversibility unknown"
            print(f"[gate] BLOCKED: {why} — human decision required. Rework the command, or run it manually if you accept the loss.", file=sys.stderr)
            log_record({
                "op": "zcode-gate",
                "tool": tool,
                                "command": git_cmd[:200],
                "input_sha256": _sha16(git_cmd),
                "task": "safety",
                "patterns_hit": destructive_hits,
                "hook_bypass": hook_bypass,
                "heads": safety_result,
"verdict": "block",
                "policy": "hitl-irreversible",
                "destructive": True,
                "reversible": reversible,
                "reversible_confidence": reversible_conf,
                "advisory_only": advisory_only,
            })
            return EXIT_BLOCK

        if advisory_only:
            print(f"[gate] advisory_only=true — proceeding with warning: {reason}", file=sys.stderr)
            log_record({
                "op": "zcode-gate",
                "tool": tool,
                                "command": git_cmd[:200],
                "input_sha256": _sha16(git_cmd),
                "task": "safety",
                "patterns_hit": destructive_hits,
                "hook_bypass": hook_bypass,
                "heads": safety_result,
"verdict": "advisory_warn",
                "destructive": True,
                "reversible": reversible,
                "reversible_confidence": reversible_conf,
                "advisory_only": True,
            })
            return EXIT_WARN
        else:
            print(f"[gate] BLOCKED: {reason}", file=sys.stderr)
            log_record({
                "op": "zcode-gate",
                "tool": tool,
                                "command": git_cmd[:200],
                "input_sha256": _sha16(git_cmd),
                "task": "safety",
                "patterns_hit": destructive_hits,
                "hook_bypass": hook_bypass,
                "heads": safety_result,
"verdict": "block",
                "destructive": True,
                "reversible": reversible,
                "reversible_confidence": reversible_conf,
                "advisory_only": False,
            })
            return EXIT_BLOCK

    # ── git commit / push gating ──────────────────────────────────────────
    if not re.match(r"git\s+(commit|push)\b", skeleton):
        # true-negative row: scanned, nothing destructive in the skeleton —
        # the calibration counterweight to the pattern hits.
        if hook_bypass:
            print("[gate] note: --no-verify bypasses hooks (advisory, non-blocking)", file=sys.stderr)
        log_record({
            "op": "zcode-gate",
            "tool": tool,
            "command": git_cmd[:200],
            "input_sha256": _sha16(git_cmd),
            "task": "safety",
            "patterns_hit": [],
            "hook_bypass": hook_bypass,
            "verdict": "clean",
            "destructive": False,
            "reversible": "unknown",
            "advisory_only": advisory_only,
        })
        return EXIT_OK

    # Determine trigger
    if "commit" in git_cmd:
        op = "scan-staged"
    else:
        op = "classify-diff"

    # Re-use the relevant subcommand
    ns = argparse.Namespace(
        trigger="zcode-pretooluse",
        provider=None,
        allow_vendor=False,
        task=None,
    )
    repo = get_repo_root()
    repo_for_log = repo_name(repo) if repo else "unknown"

    if op == "scan-staged":
        rc = cmd_scan_staged(ns)
    else:
        rc = cmd_classify_diff(ns)

    log_record({
        "op": op,
        "repo": repo_for_log,
        "trigger": "zcode-pretooluse",
        "tool": tool,
        "command": git_cmd[:200],
        "verdict": {EXIT_OK: "pass", EXIT_WARN: "warn", EXIT_BLOCK: "block"}.get(rc, "error"),
        "escalated": rc in (EXIT_WARN, EXIT_BLOCK),
    })
    return rc


def cmd_install_hooks(args: argparse.Namespace) -> int:
    repo = args.repo or str(get_repo_root() or Path.cwd())
    repo_path = Path(repo).resolve()
    hooks_path = repo_path / ".git" / "hooks"
    if not hooks_path.is_dir():
        print(f"error: {hooks_path} does not exist (not a git repo?)", file=sys.stderr)
        return EXIT_ERROR

    marker = "# dev-decisions hook — managed by dev-decisions install-hooks"

    def write_hook(name: str, cmd_template: str) -> bool:
        hook_file = hooks_path / name
        content = f"""#!/bin/sh
{marker}
# Provider API keys (git hooks don't inherit your shell env). Create
# ~/.config/dev-decisions/env with `FASTINO_API_KEY=...` lines and
# install-hooks wires it into every hook it manages.
[ -f "$HOME/.config/dev-decisions/env" ] && {{ set -a; . "$HOME/.config/dev-decisions/env"; set +a; }}
{cmd_template}
"""
        if hook_file.exists() and marker not in hook_file.read_text(errors="replace"):
            if not args.force:
                print(f"error: {hook_file} already exists (use --force to overwrite)", file=sys.stderr)
                return False
        hook_file.write_text(content)
        hook_file.chmod(0o755)
        return True

    ok = True
    cfg = load_config(repo_path)
    pre_commit_cmd = cfg["hooks"]["pre_commit"]
    pre_push_cmd = cfg["hooks"]["pre_push"]

    ok &= write_hook("pre-commit", f'exec "$HOME/.local/bin/dev-decisions" {pre_commit_cmd} --trigger git-pre-commit')
    ok &= write_hook("pre-push", f'exec "$HOME/.local/bin/dev-decisions" {pre_push_cmd} --trigger git-pre-push')

    if ok:
        print(f"Hooks installed in {hooks_path}")
        print(f"  pre-commit → {pre_commit_cmd}")
        print(f"  pre-push   → {pre_push_cmd}")
        print("Remove with: dev-decisions remove-hooks")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_remove_hooks(args: argparse.Namespace) -> int:
    repo = args.repo or str(get_repo_root() or Path.cwd())
    repo_path = Path(repo).resolve()
    hooks_path = repo_path / ".git" / "hooks"
    marker = "# dev-decisions hook — managed by dev-decisions install-hooks"
    removed = 0
    for name in ("pre-commit", "pre-push"):
        hook_file = hooks_path / name
        if hook_file.exists() and marker in hook_file.read_text(errors="replace"):
            hook_file.unlink()
            removed += 1
            print(f"Removed {name}")
    if removed == 0:
        print("No dev-decisions hooks found.")
    return EXIT_OK


def _discover_repos(root: Path, max_depth: int = 5) -> list[Path]:
    """Find git repos under root, descending up to max_depth levels."""
    repos: list[Path] = []
    if not root.is_dir():
        return repos

    def _walk(path: Path, depth: int) -> None:
        if depth >= max_depth:
            return
        try:
            children = sorted(path.iterdir())
        except (PermissionError, OSError):
            return
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / ".git").is_dir():
                repos.append(child)
            else:
                _walk(child, depth + 1)

    _walk(root, 0)
    return repos


def _is_sensitive(repo: Path) -> bool:
    """Heuristic: does this repo handle user data / credentials?"""
    local_cfg = repo / ".dev-decisions.toml"
    if local_cfg.exists():
        try:
            import tomllib
            with open(local_cfg, "rb") as f:
                if tomllib.load(f).get("repo", {}).get("sensitive", False):
                    return True
        except Exception:
            pass
    env_file = repo / ".env"
    if env_file.exists():
        try:
            text = env_file.read_text(errors="replace")
            if any(k in text for k in ("DATABASE_URL", "NEXTAUTH_SECRET", "SEED_ADMIN", "STUDENT")):
                return True
        except Exception:
            pass
    return False


def _has_dev_decisions_hooks(repo: Path) -> bool:
    marker = "# dev-decisions hook — managed by dev-decisions install-hooks"
    for name in ("pre-commit", "pre-push"):
        f = repo / ".git" / "hooks" / name
        if f.exists() and marker in f.read_text(errors="replace"):
            return True
    return False


def cmd_bulk_install(args: argparse.Namespace) -> int:
    root = Path(args.root or (Path.home() / "Projects")).expanduser().resolve()
    repos = _discover_repos(root)
    if not repos:
        print(f"No git repos found under {root}")
        return EXIT_OK

    print(f"Found {len(repos)} git repos under {root}\n")
    installed = skipped = failed = 0
    for repo in repos:
        name = repo.name
        if _has_dev_decisions_hooks(repo) and not args.force:
            print(f"  {name:<30} already installed — skip")
            skipped += 1
            continue
        hooks_path = repo / ".git" / "hooks"
        marker = "# dev-decisions hook — managed by dev-decisions install-hooks"
        try:
            for hook_name, subcmd in (("pre-commit", "scan-staged"), ("pre-push", "classify-diff")):
                hook_file = hooks_path / hook_name
                content = f"""#!/bin/sh
{marker}
exec "$HOME/.local/bin/dev-decisions" {subcmd} --trigger git-{hook_name.replace('-', '-')}
"""
                if hook_file.exists() and marker not in hook_file.read_text(errors="replace") and not args.force:
                    print(f"  {name:<30} has foreign {hook_name} — use --force to overwrite")
                    failed += 1
                    break
                hook_file.write_text(content)
                hook_file.chmod(0o755)
            else:
                sensitive = _is_sensitive(repo)
                tag = "  [sensitive]" if sensitive else ""
                print(f"  {name:<30} installed{tag}")
                installed += 1
        except Exception as e:
            print(f"  {name:<30} failed: {e}")
            failed += 1

    print(f"\n{installed} installed, {skipped} skipped, {failed} failed")
    return EXIT_OK if failed == 0 else EXIT_WARN


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root or (Path.home() / "Projects")).expanduser().resolve()
    repos = _discover_repos(root)
    if not repos:
        print(f"No git repos found under {root}")
        return EXIT_OK

    print(f"dev-decisions fleet status — {len(repos)} repos under {root}\n")
    print(f"  {'repo':<32} {'hooks':<8} {'sensitive':<10} {'env':<6} {'type':<10}")
    print(f"  {'─'*32} {'─'*8} {'─'*10} {'─'*6} {'─'*10}")
    for repo in repos:
        hooks = "yes" if _has_dev_decisions_hooks(repo) else "—"
        sensitive = "yes" if _is_sensitive(repo) else "—"
        has_env = "yes" if (repo / ".env").exists() else "—"
        if (repo / "package.json").exists():
            ptype = "js/ts"
        elif (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
            ptype = "python"
        else:
            ptype = "other"
        print(f"  {repo.name:<32} {hooks:<8} {sensitive:<10} {has_env:<6} {ptype:<10}")
    return EXIT_OK


def cmd_fleet_scan(args: argparse.Namespace) -> int:
    """Run api_drift task across the last commit of every repo under a root."""
    root = Path(args.root or (Path.home() / "Projects")).expanduser().resolve()
    repos = _discover_repos(root)
    if not repos:
        print(f"No git repos found under {root}")
        return EXIT_OK

    provider = args.provider or "local"
    task = "api_drift"
    print(f"dev-decisions fleet-scan ({task}) — {len(repos)} repos under {root}\n")
    print(f"  {'repo':<32} {'public_api':<12} {'breaking':<10} {'severity':<10} {'conf':<8}")
    print(f"  {'─'*32} {'─'*12} {'─'*10} {'─'*10} {'─'*8}")

    escalated_count = 0
    for repo in repos:
        try:
            diff = last_commit_diff(repo, max_chars=8000)
            if not diff:
                continue
            # quick heuristic: only scan repos with code-like files
            if not any(repo.rglob(f) for f in ("*.py", "*.ts", "*.js", "*.go", "*.rs", "*.java")):
                continue

            # Build a fake args namespace for classify-diff
            fake_args = argparse.Namespace(
                provider=provider,
                task=task,
                trigger="fleet-scan",
                allow_vendor=False,
            )
            # We need cfg for the provider, but we can call classify-diff directly
            # Instead, inline a minimal call to avoid re-running vendor guard
            cfg = load_config(repo)

            # Run classification (sys1 library if present, else inline providers).
            item_telemetry: dict = {}
            if sys1 is not None:
                if provider == "local" or provider == "modernbert":
                    single = _sys1_classify_single(provider, task, diff, cfg)
                    result = single.get("answers", {})
                    item_telemetry = single.get("telemetry", {})
                elif provider == "decide":
                    continue  # vendor skipped in fleet-scan
                else:
                    continue
            else:
                heads = get_task_heads(task, provider if provider != "both" else "decide")
                if provider == "local":
                    try:
                        result = _call_local_provider(diff, heads, cfg, telemetry=item_telemetry)
                    except Exception:
                        continue
                elif provider == "modernbert":
                    try:
                        result = _call_modernbert_provider(diff, heads, cfg, telemetry=item_telemetry)
                    except Exception:
                        continue
                else:
                    continue

            # Look up api_drift answers by head id (sys1) or task prefix (legacy).
            def _head(*matches: str) -> dict:
                for k, v in (result or {}).items():
                    if not isinstance(v, dict):
                        continue
                    kl = k.lower()
                    if any(m.lower() in kl for m in matches):
                        return v
                return {}
            public_api = _head("public_api_modified", "Does this diff modify any public API surface")
            breaking = _head("breaking_change", "Does this diff introduce a breaking change")
            sev_raw = _head("severity", "If there is a breaking change, how severe is it")

            def _label_of(entry: dict):
                v = entry.get("label")
                if v is None:
                    v = entry.get("noul")
                if v is None:
                    v = entry.get("score")
                if v is None:
                    return "?"
                return "yes" if v is True else "no" if v is False else str(v)

            pub_label = _label_of(public_api)
            brk_label = _label_of(breaking)
            sev_label = _label_of(sev_raw)

            # confidence from the highest-confidence head
            confs = [v.get("confidence") for v in (result or {}).values() if isinstance(v, dict) and v.get("confidence") is not None]
            conf_str = f"{max(confs):.2f}" if confs else "—"

            flag = ""
            if pub_label == "yes" or brk_label == "yes":
                flag = " ⚠"
                escalated_count += 1

            print(f"  {repo.name:<32} {pub_label:<12} {brk_label:<10} {sev_label:<10} {conf_str:<8}{flag}")

            # Log per-repo fleet-scan item for dashboard aggregation
            log_record({
                "op": "fleet-scan-item",
                "repo": repo.name,
                "task": task,
                "provider": provider,
                "public_api": pub_label,
                "breaking": brk_label,
                "severity": sev_label,
                "confidence": conf_str,
                "telemetry": item_telemetry,
            })
        except Exception:
            continue

    print(f"\n{escalated_count} repos flagged for API drift review")
    return EXIT_OK


def cmd_log(args: argparse.Namespace) -> int:
    log_path = _log_dir() / "events.jsonl"
    if not log_path.exists():
        print("No log file found.")
        return EXIT_OK

    records = []
    with open(log_path) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if args.since:
        cutoff = datetime.fromisoformat(args.since).timestamp()
        records = [r for r in records if datetime.fromisoformat(r["ts"]).timestamp() >= cutoff]

    if args.format == "json":
        print(json.dumps(records, indent=2, default=str))
    else:
        for r in records[-args.tail if args.tail else len(records):]:
            verdict = r.get("verdict", "?")
            op = r.get("op", "?")
            repo = r.get("repo", "?")
            ts = r.get("ts", "")[:19]
            escalated = r.get("escalated", False)
            esc = " ⚠" if escalated else ""
            print(f"{ts}  {op:<20} {repo:<30} {verdict}{esc}")

    return EXIT_OK


# ── GitHub layer ──────────────────────────────────────────────────────────────

def _call_gh(args: list[str], check: bool = True) -> str:
    """Thin wrapper around gh CLI. Returns stdout on success, raises on failure."""
    if shutil.which("gh") is None:
        raise RuntimeError("gh CLI not found — install https://cli.github.com/")
    try:
        r = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            check=check,
            timeout=120,
        )
        return r.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"gh failed: {e.stderr.strip() or e}") from e


def _gh_pr_number_for_branch(branch: str) -> str:
    """Resolve PR number for a branch (or current branch if None)."""
    args = ["pr", "view", "--json", "number", "-q", ".number"]
    if branch:
        args += ["--head", branch]
    out = _call_gh(args)
    if not out:
        raise RuntimeError(f"No open PR found for branch {branch or 'current'}.")
    return out


def cmd_pr_gate(args: argparse.Namespace) -> int:
    """Classify PR diff and apply labels via gh."""
    branch = getattr(args, "branch", None)
    dry_run = getattr(args, "dry_run", False)
    fanout = getattr(args, "fanout", False)
    diff_file = getattr(args, "diff_file", None)

    if diff_file:
        pr_num = None
        cfg = load_config(None)
        diff = Path(diff_file).read_text()
    else:
        if not shutil.which("gh"):
            print("error: gh CLI required (https://cli.github.com/)", file=sys.stderr)
            return EXIT_ERROR

        try:
            pr_num = _gh_pr_number_for_branch(branch)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ERROR

        cfg = load_config(None)

        # Fetch PR diff
        try:
            diff = _call_gh(["pr", "diff", pr_num])
        except RuntimeError as e:
            print(f"error: failed to fetch PR #{pr_num} diff: {e}", file=sys.stderr)
            return EXIT_ERROR

    if not diff:
        print("Diff is empty." if diff_file else f"PR #{pr_num} has no diff.")
        return EXIT_OK

    # Classify with pr_gate task
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]
    task = "pr_gate"

    fanout_meta: dict = {}
    try:
        if fanout and sys1 is not None:
            single = _sys1_classify_fanout(provider, diff, cfg)
            parsed: dict = single.get("answers", {})
            item_telemetry: dict = single.get("telemetry", {})
            fanout_meta = single
        elif sys1 is not None:
            single = _sys1_classify_single(provider, task, diff, cfg)
            parsed = single.get("answers", {})
            item_telemetry = single.get("telemetry", {})
        else:
            item_telemetry = {}
            heads = get_task_heads(task, provider.split("+")[0])
            if provider in ("decide", "both"):
                result = _call_openai_compatible(
                    cfg["providers"]["decide_api_url"],
                    _env_key("decide"),
                    cfg["providers"]["decide_model"],
                    [
                        {"role": "system", "content": "You are a PR classifier. Answer only with the requested labels."},
                        {"role": "user", "content": f"Review this PR diff and classify it.\n\nDiff:\n{diff}"},
                    ],
                    schema={"classifications": heads},
                    timeout=cfg["providers"]["request_timeout_seconds"],
                    max_retries=cfg["providers"]["max_retries"],
                    backoff=cfg["providers"]["retry_backoff_seconds"],
                    telemetry=item_telemetry,
                )
                parsed = _parse_decide_response(result)
            elif provider == "jev":
                questions = get_task_heads(task, "jev")
                payload_questions = {q["id"]: {"type": q["type"], "instructions": q["instructions"], "criteria": q.get("criteria")} for q in questions}
                body = json.dumps({"state": {"diff": diff[:DEFAULT_MAX_DIFF_CHARS], "task": task}, "questions": payload_questions, "model": cfg["providers"]["jev_model"]})
                resp = _call_jev_raw(body, _env_key("jev"), cfg, telemetry=item_telemetry)
                parsed = _parse_jev_response(resp, [q["id"] for q in questions])
            elif provider == "modernbert":
                parsed = _call_modernbert_provider(diff, heads, cfg, telemetry=item_telemetry)
            else:
                parsed = _call_local_provider(diff, heads, cfg, telemetry=item_telemetry)
    except Exception as e:
        print(f"error: classification failed: {e}", file=sys.stderr)
        return EXIT_ERROR

    # Extract labels (multi-label support). With --fanout, restrict to the
    # three standard heads so per-file action choices never become PR labels.
    if fanout:
        labels_to_apply = _extract_labels(parsed, prefer_key_substrings=("diff_type", "risk_tier", "suggested_labels"))
    else:
        labels_to_apply = _extract_labels(parsed) if sys1 is not None else None
    if labels_to_apply is None:
        labels_to_apply = []
        for key, val in parsed.items():
            if isinstance(val, dict):
                label = val.get("label")
                if label:
                    if isinstance(label, list):
                        labels_to_apply.extend(label)
                    else:
                        labels_to_apply.append(label)
        labels_to_apply = sorted(set(labels_to_apply))

    subject = f"PR #{pr_num}" if pr_num is not None else "Diff"
    print(f"{subject} classification:")
    print(f"  labels: {', '.join(labels_to_apply) or 'none'}")

    if fanout_meta:
        print(f"  per-file fan-out ({fanout_meta.get('provider')}, {fanout_meta.get('latency_ms')} ms, {len(fanout_meta.get('files', []))} file heads):")
        for i, path in enumerate(fanout_meta.get("files", [])):
            risky = parsed.get(f"f{i}_risky", {}) or {}
            action = parsed.get(f"f{i}_action", {}) or {}
            risk = risky.get("noul")
            act = action.get("label") or "----"
            risk_s = f"{risk:.2f}" if isinstance(risk, (int, float)) else "----"
            print(f"    {act:>7}  risk={risk_s}  {path}")
        omitted = fanout_meta.get("omitted") or []
        if omitted:
            shown = ", ".join(omitted[:5]) + ("..." if len(omitted) > 5 else "")
            print(f"    (no heads for {len(omitted)} more files: {shown})")

    if dry_run or pr_num is None:
        if dry_run:
            print("  (dry-run — labels not applied)")
        else:
            print("  (diff-file mode — labels not applied)")
        return EXIT_OK

    # Apply labels
    if labels_to_apply:
        for label in labels_to_apply:
            try:
                _call_gh(["pr", "edit", pr_num, "--add-label", label])
                print(f"  ✓ applied label: {label}")
            except RuntimeError as e:
                print(f"  ✗ failed to apply {label}: {e}", file=sys.stderr)

    log_record({
        "op": "pr-gate",
        "pr": pr_num,
        "fanout": bool(fanout_meta),
        "repo": repo_name(get_repo_root() or Path(".")),
        "task": task,
        "provider": provider,
        "labels": labels_to_apply,
        "dry_run": dry_run,
        "verdict": "pass" if not labels_to_apply else "applied",
        "telemetry": item_telemetry,
    })
    return EXIT_OK


_PLAN_GATE_MAX_CRITERIA = 12
_PLAN_GATE_MAX_SECTIONS = 14
_PLAN_GATE_MAX_TEST_FILES = 20
_PLAN_GATE_TESTS_PER_FILE = 12
_EVIDENCE_GATE_MAX_CRITERIA = 12
# Evidence bundles are the long-input case: full logs, not tails. Drex capacity
# governs the state bound; smaller-window wires (jev fallback) truncate to
# their own bounds internally.
_EVIDENCE_BLOCK_CHARS = 20_000
_EVIDENCE_VERDICTS = ("supported", "insufficient", "contradicted")
_DOCS_GATE_MAX_ARTIFACTS = 8
_DOCS_GATE_MAX_SECTIONS = 16
_DOCS_GATE_SECTION_CHARS = 2000
_DOCS_GATE_MAX_STALE = 8
_ARCH_GATE_MAX_CLAIMS = 12
_ARCH_GATE_DOC_SECTIONS = 20
_ARCH_GATE_DOC_SECTION_CHARS = 2000
_ARCH_GATE_CLAIM_CHARS = 300
_ARCH_GATE_VERDICTS = ("conforms", "drifts", "undocumented")
_ARCH_GATE_META_SKIP = ("acceptance criteria", "outcome", "verification", "linked artifacts", "out of scope", "status", "context")
_PLAN_GATE_SECTION_CHARS = 2000
# Meta sections carry no proposed work; scope-creep heads would false-flag them.
_PLAN_GATE_SKIP_SECTIONS = ("acceptance criteria", "outcome", "out of scope", "verification", "linked artifacts", "files to be touched", "status", "context", "notes")


def _parse_plan_sections(text: str) -> list[tuple[str, str]]:
    """Split plan markdown into (heading, body) pairs at '## ' and '### ' headings.

    Splitting at ### too keeps phases/subsections in their own sections, so the
    per-section character bound never silently truncates the evidence for a
    later criterion (the context-rot edge).
    """
    sections: list[tuple[str, str]] = []
    heading: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if (line.startswith("## ") or line.startswith("### ")) and not line.startswith("####"):
            if heading is not None:
                sections.append((heading, "\n".join(body).strip()))
            heading = line.lstrip("#").strip()
            body = []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(body).strip()))
    return sections


def _parse_plan_criteria(text: str) -> list[str]:
    """Checkbox lines ('- [ ]' / '- [x]') are the acceptance criteria."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*-\s+\[[ xX]\]\s+(.+)$", line)
        if m:
            out.append(m.group(1).strip())
    return out


def cmd_plan_gate(args: argparse.Namespace) -> int:
    """
    Gate a plan against its acceptance criteria (the plan-as-contract check):
      - coverage: per-criterion noul — does the plan body satisfy it?
      - verifiability: per-criterion choice — observable / partial / unverifiable
      - scope creep: per-work-section noul — does any criterion require it?
    Counting happens in code (never the model); exit 1 when gaps exist.
    """
    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return EXIT_ERROR
    if sys1 is None:
        print("error: plan-gate requires sys1", file=sys.stderr)
        return EXIT_ERROR

    text = plan_path.read_text()
    if args.criteria_file:
        criteria = [ln.strip().lstrip("-").strip() for ln in Path(args.criteria_file).read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    else:
        criteria = _parse_plan_criteria(text)
    if not criteria:
        print("error: no acceptance criteria found (checkbox lines) and no --criteria-file given.", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(None)
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]

    overflow = criteria[_PLAN_GATE_MAX_CRITERIA:]
    criteria = criteria[:_PLAN_GATE_MAX_CRITERIA]
    all_sections = _parse_plan_sections(text)
    sections = [(h, b) for h, b in all_sections if not any(k in h.lower() for k in _PLAN_GATE_SKIP_SECTIONS)][:_PLAN_GATE_MAX_SECTIONS]

    # Test inventory (mechanical collection; sys1 only does the semantic matching)
    test_root = getattr(args, "tests", None)
    test_files: list[tuple[str, list[str]]] = []
    test_files_total = 0
    if test_root:
        root = Path(test_root)
        if not root.exists():
            print(f"error: --tests root not found: {root}", file=sys.stderr)
            return EXIT_ERROR
        skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__"}
        for p in sorted(root.rglob("*.py")):
            if skip_dirs & set(p.parts):
                continue
            if not (p.name.startswith("test_") or p.name.endswith("_test.py")):
                continue
            try:
                tests = re.findall(r"^\s*def (test_\w+)", p.read_text(), re.M)
            except Exception:
                continue
            if tests:
                test_files.append((str(p.relative_to(root)), tests))
        test_files_total = len(test_files)
        if test_files_total > _PLAN_GATE_MAX_TEST_FILES:
            test_files = test_files[:_PLAN_GATE_MAX_TEST_FILES]  # overflow noted in the report

    heads: list = []
    for i, c in enumerate(criteria):
        heads.append(sys1.make_noul(
            f"Does the plan body contain steps that satisfy acceptance criterion C{i}? Criterion: {c}",
            id=f"c{i}_covered"))
        heads.append(sys1.make_choice(
            f"Does the plan state an observable outcome that would settle acceptance criterion C{i}?",
            ["observable", "partial", "unverifiable"],
            id=f"c{i}_verifiable",
            descriptions={
                "observable": "The plan names a concrete checkable result (file, behavior, or test) for this criterion.",
                "partial": "The plan gestures at the criterion without a checkable result.",
                "unverifiable": "The plan gives no way to tell the criterion was satisfied.",
            }))
    for j, (h, _b) in enumerate(sections):
        heads.append(sys1.make_noul(
            f"Does at least one acceptance criterion (C0..C{len(criteria) - 1} listed in the state) require the work described in section s{j} ({h})?",
            id=f"s{j}_in_scope"))
    test_options: dict[str, str] = {}
    if test_files:
        test_options = {f"tf{j}": f"{rel} — {len(ts)} tests ({', '.join(ts[:6])}{'…' if len(ts) > 6 else ''})" for j, (rel, ts) in enumerate(test_files)}
        test_options["none"] = "No test in the inventory exercises this criterion."
        for i, c in enumerate(criteria):
            heads.append(sys1.make_noul(
                f"Does the test inventory in the state contain tests that verify the behavior acceptance criterion C{i} describes? Criterion: {c}",
                id=f"t{i}_tested"))
            heads.append(sys1.make_choice(
                f"Which test file best matches acceptance criterion C{i}?",
                list(test_options.keys()), id=f"t{i}_where", descriptions=test_options))

    parts = [f"A development plan with {len(criteria)} acceptance criteria (C0..C{len(criteria) - 1}) and {len(sections)} work sections (s0..s{len(sections) - 1})."]
    for i, c in enumerate(criteria):
        parts.append(f"C{i}: {c}")
    for j, (h, b) in enumerate(sections):
        parts.append(f"=== SECTION s{j}: {h} ===\n{b[:_PLAN_GATE_SECTION_CHARS]}")
    if test_files:
        parts.append(f"TEST INVENTORY — {len(test_files)} test files (ids tf0..tf{len(test_files) - 1}):")
        for j, (rel, ts) in enumerate(test_files):
            parts.append(f"tf{j} = {rel} ({len(ts)} tests): {', '.join(ts[:_PLAN_GATE_TESTS_PER_FILE])}{'…' if len(ts) > _PLAN_GATE_TESTS_PER_FILE else ''}")
    if overflow:
        parts.append("Criteria beyond the head limit have no heads; context only: " + " | ".join(overflow))
    state = "\n\n".join(parts)[: cfg["scan"]["max_diff_chars"]]

    task = sys1.types.Task(id="plan_gate", heads=heads, description="Plan vs acceptance-criteria coverage gate (speculative fan-out)")
    chain = _sys1_chain(provider) if provider != "auto" else sys1.routing.route_decision(task, state, cfg)[0] or _sys1_chain("jev")
    n_draws = max(1, int(getattr(args, "draws", 3) or 3))
    answers, used_provider, used_latency, draws_used, used_telemetry = _sys1_consistency(
        chain, task, state, cfg, n=n_draws)
    if not answers:
        print(f"error: no provider answered (chain: {', '.join(chain)})", file=sys.stderr)
        return EXIT_ERROR

    def _noul_p(head_id: str):
        a = answers.get(head_id) or {}
        p = a.get("noul")
        return p if isinstance(p, (int, float)) else None

    missing, creep, unverifiable = [], [], []
    for i, c in enumerate(criteria):
        p = _noul_p(f"c{i}_covered")
        if p is None or p < 0.5:
            unstable = " /UNSTABLE" if (answers.get(f"c{i}_covered") or {}).get("unstable") else ""
            missing.append(f"    [MISSING{unstable}]     C{i} (p={p if p is not None else '----'}) {c[:110]}")
        v = (answers.get(f"c{i}_verifiable") or {}).get("label")
        if v == "unverifiable":
            unverifiable.append(f"    [UNVERIFIABLE] C{i}: {c[:110]}")
    for j, (h, _b) in enumerate(sections):
        p = _noul_p(f"s{j}_in_scope")
        if p is None or p < 0.5:
            creep.append(f"    [SCOPE?]      s{j} (p={p if p is not None else '----'}) {h}")

    print(f"Plan gate: {plan_path.name}  (provider={used_provider}, {used_latency} ms, {len(criteria)} criteria, {len(sections)} sections)")
    print(f"  coverage: {len(criteria) - len(missing)}/{len(criteria)} covered")
    for line in missing:
        print(line)
    for line in unverifiable:
        print(line)
    print(f"  scope: {len(sections) - len(creep)}/{len(sections)} in scope")
    for line in creep:
        print(line)
    untested: list[int] = []
    if test_files:
        print(f"  test coverage: {len(criteria)} criteria vs {len(test_files)} test files" + (f" ({test_files_total} found)" if test_files_total > len(test_files) else ""))
        for i, c in enumerate(criteria):
            p = _noul_p(f"t{i}_tested")
            where = (answers.get(f"t{i}_where") or {}).get("label") or ""
            rel = ""
            if where.startswith("tf"):
                try:
                    rel = test_files[int(where[2:])][0]
                except (IndexError, ValueError):
                    rel = where
            if p is not None and p >= 0.5 and rel:
                print(f"    C{i} -> {rel}")
            else:
                untested.append(i)
                print(f"    [UNTESTED]    C{i}  {c[:100]}")
    if overflow:
        print(f"  (no heads for {len(overflow)} more criteria)")

    gaps = bool(missing or unverifiable or creep or untested)
    log_record({
        "op": "plan-gate",
        "plan": str(plan_path),
        "provider": used_provider,
        "task": "plan_gate",
        "input_sha256": _sha16(state),
        "draws": draws_used,
        "criteria": len(criteria),
        "sections": len(sections),
        "uncovered": [i for i in range(len(criteria)) if (_noul_p(f"c{i}_covered") or 0) < 0.5],
        "creep_sections": [j for j in range(len(sections)) if (_noul_p(f"s{j}_in_scope") or 0) < 0.5],
        "unstable": [i for i in range(len(criteria))
                     if (answers.get(f"c{i}_covered") or {}).get("unstable")],
        "heads": {hid: {k: a[k] for k in ("noul", "label", "confidence", "mean", "stdev", "unstable", "draws") if k in a}
                  for hid, a in answers.items() if isinstance(a, dict) and not a.get("_declined")},
        "untested_criteria": untested,
        "test_files": len(test_files),
        "verdict": "gaps" if gaps else "pass",
        "latency_ms": used_latency,
        "telemetry": used_telemetry,
    })
    print(f"  verdict: {'GAPS' if gaps else 'PASS'}")
    return EXIT_WARN if gaps else EXIT_OK



def cmd_plan_surface(args: argparse.Namespace) -> int:
    """
    Feed-forward plan surface (layered sys1 precompute for task decomposition):
      - map layer: criterion -> repository surface modules (choice over a
        numbered inventory + existence noul per criterion, one request)
      - deps layer: pairwise criterion ordering (one choice head per unordered
        pair, chunked across requests)
      - assembly, no model: thresholded DAG, topological order, uncertain band
        reported (never silently dropped), weakest-edge cycle breaks, path-
        pattern risk flags.
    Advisorial input to the decomposer, not a gate: it may overrule any edge
    or mapping; record overrules with `disposition plan-surface <plan>
    --status overridden --reason ...` so they double as calibration rows.
    """
    if _plansurface is None:
        print("error: plan-surface requires sys1 with the plansurface module (upgrade sys1)", file=sys.stderr)
        return EXIT_ERROR
    plan_path = Path(args.plan).expanduser()
    if not plan_path.exists():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return EXIT_ERROR
    repo_root = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    if not repo_root.exists():
        print(f"error: repo root not found: {repo_root}", file=sys.stderr)
        return EXIT_ERROR

    plan_text = plan_path.read_text()
    criteria = _parse_plan_criteria(plan_text)[:_plansurface.MAX_CRITERIA]
    if not criteria:
        print("error: no acceptance criteria found (checkbox lines).", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(None)
    map_floor = args.map_floor if args.map_floor is not None else _plansurface.MAP_FLOOR
    map_top = args.map_top if args.map_top is not None else _plansurface.MAP_TOP
    dep_threshold = args.dep_threshold if args.dep_threshold is not None else _plansurface.DEP_THRESHOLD
    band = args.band if args.band is not None else _plansurface.DEP_BAND

    modules = _plansurface.inventory_surface(repo_root)
    print(f"Plan surface: {plan_path.name}  ({len(criteria)} criteria, {len(modules)} inventory entries)")
    map_res = _plansurface.run_map(criteria, modules, provider=args.map_provider, cfg=cfg)
    if not map_res["answers"]:
        print(f"error: map layer returned no answers (chain: {', '.join(map_res['chain'])})", file=sys.stderr)
        return EXIT_ERROR
    dep_res = _plansurface.run_deps(criteria, provider=args.deps_provider, cfg=cfg)
    if not dep_res["answers"]:
        print(f"error: deps layer returned no answers (chain: {', '.join(dep_res['chain'])})", file=sys.stderr)
        return EXIT_ERROR

    artifact = _plansurface.assemble(map_res, dep_res, criteria, modules,
                                     map_floor=map_floor, map_top=map_top,
                                     dep_threshold=dep_threshold, band=band)
    artifact["meta"]["plan"] = str(plan_path)
    artifact["meta"]["repo_root"] = str(repo_root)
    artifact["meta"]["criteria_texts"] = criteria

    SURFACES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SURFACES_DIR / f"{plan_path.stem}.surface.json"
    out_path.write_text(json.dumps(artifact, indent=2))

    pmap, pdeps = artifact["meta"]["provider_map"], artifact["meta"]["provider_deps"]
    order = artifact["order"]
    print(f"  map: {sum(1 for c in artifact['criteria'] if c['modules'])}/{len(criteria)} criteria mapped"
          f" (provider={pmap})")
    for node in artifact["criteria"]:
        mods = ", ".join(f"{m['path']} ({m['p']})" for m in node["modules"]) or "unmapped"
        risks = f"  [risk: {', '.join(node['risks'])}]" if node["risks"] else ""
        print(f"    {node['id']}: {mods}{risks}")
    print(f"  dependencies: {len(artifact['edges'])} edges above {dep_threshold}, "
          f"{len(artifact['uncertain'])} uncertain (provider={pdeps})")
    for e in artifact["edges"]:
        print(f"    {e['before']} -> {e['after']}  p={e['p']}")
    for u in artifact["uncertain"]:
        print(f"    [UNCERTAIN] {'/'.join(u['pair'])}  reading={u['reading']}")
    for cb in artifact["cycle_breaks"]:
        d = cb["dropped"]
        print(f"    [CYCLE] dropped {d['before']} -> {d['after']} (p={d['p']}) to break a cycle")
    # A near-chain predicted order is the over-serialization signature: present
    # the ordering as SOFT (edge list above is the truth, this is one linearization).
    print(f"  suggested order (soft, one linearization of the edges): {' -> '.join(order) if order else '(none)'}")
    print(f"  artifact: {out_path}")
    print("  overrule anything with: dev-decisions disposition plan-surface "
          f"{plan_path} --status overridden --reason ...")

    log_record({
        "op": "plan-surface",
        "plan": str(plan_path),
        "repo": str(repo_root),
        "provider": f"map={pmap},deps={pdeps}",
        "task": "plan_surface_map+plan_deps",
        "criteria": len(criteria),
        "modules": len(modules),
        "edges": len(artifact["edges"]),
        "uncertain": len(artifact["uncertain"]),
        "cycle_breaks": len(artifact["cycle_breaks"]),
        "order": order,
        "artifact": str(out_path),
        "input_sha256": map_res["sha256"],
        "heads": {
            pmap: {k: {"confidence": v.get("confidence")}
                   for k, v in map_res["answers"].items() if isinstance(v, dict)},
            pdeps: {k: {"confidence": v.get("confidence")}
                    for k, v in dep_res["answers"].items() if isinstance(v, dict)},
        },
        "telemetry": {"map": map_res["telemetry"], "deps": dep_res["telemetry"]},
        "verdict": "ok",
    })
    return EXIT_OK


def _parse_evidence_blocks(text: str) -> dict[str, str]:
    """Parse '== C<i> ==' tagged evidence blocks into {id: text} (bounded per block)."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^\s*==\s*(C\d+)\s*==", line)
        if m:
            current = m.group(1)
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(line)
    out: dict[str, str] = {}
    for k, lines in blocks.items():
        joined = "\n".join(lines).strip()
        if joined:
            out[k] = joined[:_EVIDENCE_BLOCK_CHARS]
    return out


def cmd_evidence_gate(args: argparse.Namespace) -> int:
    """
    QA evidence gate for close-out: does the collected evidence support a pass
    per acceptance criterion? Three verdicts per criterion: supported,
    insufficient, contradicted. Fail-closed — a criterion without evidence is
    never a pass, and the gate downgrades passes but never overturns a
    deterministic failure (a red test stays red; this gate judges evidence).
    """
    plan_path = Path(args.plan)
    evidence_path = Path(args.evidence)
    if not plan_path.exists():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return EXIT_ERROR
    if not evidence_path.exists():
        print(f"error: evidence file not found: {evidence_path}", file=sys.stderr)
        return EXIT_ERROR
    if sys1 is None:
        print("error: evidence-gate requires sys1", file=sys.stderr)
        return EXIT_ERROR

    text = plan_path.read_text()
    criteria = _parse_plan_criteria(text)[:_EVIDENCE_GATE_MAX_CRITERIA]
    if not criteria:
        print("error: no acceptance criteria found in plan (checkbox lines).", file=sys.stderr)
        return EXIT_ERROR
    blocks = _parse_evidence_blocks(evidence_path.read_text())
    judged = [i for i in range(len(criteria)) if f"C{i}" in blocks]
    no_evidence = [i for i in range(len(criteria)) if f"C{i}" not in blocks]
    if not judged:
        print("error: no evidence blocks found. Tag them '== C0 ==' etc., matching the plan's checkbox order.", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(None)
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]

    heads: list = []
    for i in judged:
        heads.append(sys1.make_noul(
            f"Does the evidence for criterion C{i} demonstrate the criterion's observable outcome? Criterion: {criteria[i]}",
            id=f"c{i}_sufficient"))
        heads.append(sys1.make_noul(
            f"Is the evidence for criterion C{i} free of contradictions with the claim (skipped tests, wrong build or environment, errors the summary ignored, retried-until-pass)?",
            id=f"c{i}_consistent"))
        heads.append(sys1.make_choice(
            f"Verdict for criterion C{i} based on its evidence?",
            list(_EVIDENCE_VERDICTS),
            id=f"c{i}_verdict",
            descriptions={
                "supported": "The evidence demonstrates the outcome with no contradictions.",
                "insufficient": "The evidence does not fully demonstrate the outcome; more is needed.",
                "contradicted": "The evidence contains contradictions with the claim.",
            }))

    parts = [f"QA close-out for a plan with {len(criteria)} acceptance criteria (C0..C{len(criteria) - 1}). Evidence blocks tagged == C<i> == follow."]
    for i, c in enumerate(criteria):
        parts.append(f"C{i}: {c}")
    for i in judged:
        parts.append(f"=== EVIDENCE C{i} ===\n{blocks[f'C{i}']}")
    # Evidence bundles are the long-input case; Drex capacity governs, and
    # smaller-window wires (jev fallback) truncate to their own bounds.
    state = "\n\n".join(parts)[: cfg["providers"].get("drex_max_chars", 400_000)]

    task = sys1.types.Task(id="evidence_gate", heads=heads, description="QA evidence sufficiency/consistency gate (fail-closed)")
    chain = _sys1_chain(provider) if provider != "auto" else sys1.routing.route_decision(task, state, cfg)[0] or _sys1_chain("jev")
    result = sys1.classify(chain, task, state, cfg=cfg, log=False)
    answers: dict = {}
    used_provider = chain[0] if chain else ""
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        if result.answers.get(mapped):
            answers = result.answers[mapped]
            used_provider = mapped
            break
    if not answers:
        print(f"error: no provider answered (chain: {', '.join(chain)})", file=sys.stderr)
        return EXIT_ERROR

    def _noul_p(head_id: str):
        a = answers.get(head_id) or {}
        p = a.get("noul")
        return p if isinstance(p, (int, float)) else None

    print(f"Evidence gate: {plan_path.name} vs {evidence_path.name}  (provider={used_provider}, {result.latency_ms} ms, {len(judged)} judged, {len(no_evidence)} without evidence)")
    not_supported: list[tuple[int, str]] = []
    for i in range(len(criteria)):
        if i in no_evidence:
            print(f"    C{i}: NO EVIDENCE   {criteria[i][:100]}")
            not_supported.append((i, "no-evidence"))
            continue
        s = _noul_p(f"c{i}_sufficient")
        con = _noul_p(f"c{i}_consistent")
        v = (answers.get(f"c{i}_verdict") or {}).get("label") or "insufficient"
        # Fail-closed override: the verdict Choice and the nouls can disagree
        # (no structural invariants — jev-1.13 jaggedness). A strong
        # inconsistency or insufficiency signal downgrades the verdict in code,
        # never the reverse. Thresholds are illustrative; fit from JSONL.
        if isinstance(con, (int, float)) and con < 0.4 and v == "supported":
            v = "contradicted"
        if isinstance(s, (int, float)) and s < 0.4 and v == "supported":
            v = "insufficient"
        s_s = f"{s:.2f}" if isinstance(s, (int, float)) else "----"
        c_s = f"{con:.2f}" if isinstance(con, (int, float)) else "----"
        print(f"    C{i}: {v.upper():<12} (sufficiency={s_s}, consistency={c_s})  {criteria[i][:90]}")
        if v != "supported":
            not_supported.append((i, v))

    gaps = bool(not_supported)
    log_record({
        "op": "evidence-gate",
        "plan": str(plan_path),
        "evidence": str(evidence_path),
        "provider": used_provider,
        "task": "evidence_gate",
        "input_sha256": _sha16(state),
        "heads": answers,
        "judged": len(judged),
        "no_evidence": no_evidence,
        "not_supported": not_supported,
        "verdict": "not-supported" if gaps else "supported",
        "latency_ms": result.latency_ms,
        "telemetry": result.telemetry.get(used_provider, {}),
    })
    print(f"  verdict: {'NOT SUPPORTED' if gaps else 'SUPPORTED'}")
    return EXIT_WARN if gaps else EXIT_OK


def _parse_linked_artifacts(text: str) -> list[tuple[str, str]]:
    """Parse the '## Linked artifacts' section into (doc path, promise) pairs.

    Only backticked file paths count; directories (trailing '/') and free-text
    lines are skipped.
    """
    artifacts: list[tuple[str, str]] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = "linked artifact" in line.lower()
            continue
        if not in_section:
            continue
        m = re.match(r"^\s*-\s+`([^`]+)`\s+[—–-]+\s+(.+)$", line)
        if m and not m.group(1).rstrip().endswith("/"):
            artifacts.append((m.group(1), m.group(2).strip()))
    return artifacts


def _removed_md_claims(diff_text: str) -> list[str]:
    """Removed lines from markdown files in a diff = old doc claims (bounded)."""
    claims: list[str] = []
    cur_md = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            cur_md = line.rstrip().endswith(".md")
            continue
        if cur_md and line.startswith("-") and not line.startswith("---"):
            claim = line[1:].strip().lstrip("-").strip()
            if len(claim) > 40 and not claim.startswith(("#", "!", "[", "|")):
                claims.append(claim[:200])
    return claims[:_DOCS_GATE_MAX_STALE]


def cmd_docs_gate(args: argparse.Namespace) -> int:
    """
    Documentation coverage gate: does the documentation fully cover what was
    developed or fixed? Two directions — forward coverage (does each linked
    artifact now contain its promised update) and change-induced staleness
    (do the docs still assert claims the diff removed). Advisorial; the gate
    reads docs and diffs, never verifies prose quality.
    """
    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return EXIT_ERROR
    if sys1 is None:
        print("error: docs-gate requires sys1", file=sys.stderr)
        return EXIT_ERROR

    text = plan_path.read_text()
    repo_root = Path(getattr(args, "repo_root", None) or Path.cwd())
    artifacts = _parse_linked_artifacts(text)[:_DOCS_GATE_MAX_ARTIFACTS]
    if not artifacts:
        print("error: no linked artifacts found — expected backticked doc paths with a promise under '## Linked artifacts'.", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(None)
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]

    diff_value = getattr(args, "diff", None)
    diff_text = ""
    if diff_value:
        dp = Path(diff_value)
        if dp.exists():
            diff_text = dp.read_text()
        else:
            out = subprocess.run(["git", "-C", str(repo_root), "diff", diff_value], capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                print(f"error: git diff {diff_value} failed: {out.stderr.strip()[:150]}", file=sys.stderr)
                return EXIT_ERROR
            diff_text = out.stdout

    # Dedupe docs by path — the same file can serve several artifacts.
    doc_sections: dict[str, list[tuple[str, str]]] = {}
    doc_missing: set[str] = set()
    for rel, _promise in artifacts:
        if rel in doc_sections or rel in doc_missing:
            continue
        p = repo_root / rel
        if p.exists() and p.is_file():
            doc_sections[rel] = _parse_plan_sections(p.read_text())[:_DOCS_GATE_MAX_SECTIONS]
        else:
            doc_missing.add(rel)
    doc_rels = [rel for rel, _ in artifacts if rel in doc_sections]
    doc_missing = sorted(doc_missing)

    # Request 1 — coverage: each artifact promise vs its document's sections.
    # Focused state: artifacts + doc sections, nothing else (context rot).
    cov_heads: list = []
    for i, (rel, promise) in enumerate(artifacts):
        if rel in doc_missing:
            continue
        j = doc_rels.index(rel)
        secs = doc_sections[rel]
        cov_heads.append(sys1.make_noul(
            f"Does the document {rel} now cover this promised update? Promise: {promise}",
            id=f"a{i}_updated"))
        options = {f"d{j}.{s}": h for s, (h, _b) in enumerate(secs)}
        options["none"] = "No section in this document covers the promise."
        cov_heads.append(sys1.make_choice(
            f"Which section of {rel} covers the promised update?",
            list(options.keys()), id=f"a{i}_where", descriptions=options))
    parts = [f"Documentation coverage check: {len(artifacts)} promised updates (A0..A{len(artifacts) - 1}) against their documents' sections (d<j>.<s>)."]
    for i, (rel, promise) in enumerate(artifacts):
        parts.append(f"A{i} -> {rel}: {promise}")
    for j, rel in enumerate(doc_rels):
        for s, (h, b) in enumerate(doc_sections[rel]):
            parts.append(f"=== d{j}.{s} [{h}] ({rel}) ===\n{b[:_DOCS_GATE_SECTION_CHARS]}")
    cov_state = "\n\n".join(parts)[: cfg["providers"].get("drex_max_chars", 400_000)]

    cov_task = sys1.types.Task(id="docs_gate", heads=cov_heads, description="Documentation coverage gate (speculative fan-out)")
    chain = _sys1_chain(provider) if provider != "auto" else sys1.routing.route_decision(cov_task, cov_state, cfg)[0] or _sys1_chain("jev")
    result = sys1.classify(chain, cov_task, cov_state, cfg=cfg, log=False)
    answers: dict = {}
    used_provider = chain[0] if chain else ""
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        if result.answers.get(mapped):
            answers = result.answers[mapped]
            used_provider = mapped
            break
    if not answers:
        print(f"error: no provider answered (chain: {', '.join(chain)})", file=sys.stderr)
        return EXIT_ERROR

    # Request 2 — staleness: removed claims vs the docs, in a state that
    # carries no coverage framing (mixing the two drowned the noul: 0.01 on a
    # verbatim match, vs 0.78-0.90 in a focused state — measured 2026-10-02).
    stale_claims = _removed_md_claims(diff_text) if diff_text else []
    stale_answers: dict = {}
    stale_provider = ""
    stale_latency = 0
    if stale_claims:
        sparts = [f"{len(stale_claims)} documentation claims were removed by a change. For each removed claim, judge whether any document below still asserts it."]
        for j, rel in enumerate(doc_rels):
            for s, (h, b) in enumerate(doc_sections[rel]):
                sparts.append(f"=== d{j}.{s} [{h}] ({rel}) ===\n{b[:_DOCS_GATE_SECTION_CHARS]}")
        for k, claim in enumerate(stale_claims):
            sparts.append(f"REMOVED CLAIM x{k}: {claim}")
        stale_state = "\n\n".join(sparts)[: cfg["providers"].get("drex_max_chars", 400_000)]
        stale_heads = [sys1.make_noul(
            f"The diff removed this documentation claim. Do any of the documents in the state still assert it? Removed claim: {claim}",
            id=f"x{k}_stale") for k, claim in enumerate(stale_claims)]
        stale_task = sys1.types.Task(id="docs_gate_stale", heads=stale_heads, description="Removed-claim staleness check")
        schain = _sys1_chain(provider) if provider != "auto" else sys1.routing.route_decision(stale_task, stale_state, cfg)[0] or _sys1_chain("jev")
        sresult = sys1.classify(schain, stale_task, stale_state, cfg=cfg, log=False)
        for pid in schain:
            mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
            if sresult.answers.get(mapped):
                stale_answers = sresult.answers[mapped]
                stale_provider = mapped
                stale_latency = sresult.latency_ms
                break
    answers.update(stale_answers)

    def _noul_p(head_id: str):
        a = answers.get(head_id) or {}
        p = a.get("noul")
        return p if isinstance(p, (int, float)) else None

    providers_used = "+".join(filter(None, dict.fromkeys([used_provider, stale_provider])))
    print(f"Docs gate: {plan_path.name}  (provider={providers_used or 'none'}, coverage={result.latency_ms} ms"
          + (f", staleness={stale_latency} ms" if stale_claims else "") + f", {len(doc_rels)} docs, {len(stale_claims)} staleness claims)")
    uncovered: list[int] = []
    for i, (rel, promise) in enumerate(artifacts):
        if rel in doc_missing:
            uncovered.append(i)
            print(f"    [NO DOC]      A{i} {rel} — {promise[:90]}")
            continue
        p = _noul_p(f"a{i}_updated")
        where = (answers.get(f"a{i}_where") or {}).get("label") or ""
        if p is not None and p >= 0.5 and where != "none":
            j = doc_rels.index(rel)
            heading = doc_sections[rel][int(where.split(".")[-1])][0] if where.startswith(f"d{j}.") else where
            print(f"    A{i} COVERED by [{heading}] ({rel})")
        elif p is not None and p >= 0.5:
            print(f"    A{i} COVERED (doc-level; no specific section) ({rel})")
        else:
            uncovered.append(i)
            print(f"    [NOT COVERED] A{i} (p={p if p is not None else '----'}) {rel} — {promise[:90]}")
    stale_hits = []
    for k, claim in enumerate(stale_claims):
        p = _noul_p(f"x{k}_stale")
        p_s = f"{p:.2f}" if isinstance(p, (int, float)) else "----"
        if p is not None and p >= 0.5:
            stale_hits.append(k)
            print(f"    [STALE]       x{k} (p={p_s}) still asserted: {claim[:100]}")
        else:
            print(f"    [ok]          x{k} (p={p_s}) removed claim not re-asserted: {claim[:80]}")
    if not stale_claims:
        print("    staleness: no diff supplied — skipped")

    gaps = bool(uncovered or stale_hits)
    log_record({
        "op": "docs-gate",
        "plan": str(plan_path),
        "provider": providers_used,
        "task": "docs_gate",
        "input_sha256": _sha16(cov_state),
        "heads": answers,
        "artifacts": len(artifacts),
        "uncovered": uncovered,
        "stale_claims": stale_hits,
        "verdict": "gaps" if gaps else "pass",
        "latency_ms": result.latency_ms,
        "telemetry": result.telemetry.get(used_provider, {}),
    })
    print(f"  verdict: {'GAPS' if gaps else 'PASS'}")
    return EXIT_WARN if gaps else EXIT_OK


def _arch_gate_claims(text: str) -> list[str]:
    """Architectural claims from a plan: non-meta section leads + file-touch lines."""
    claims: list[str] = []
    for h, b in _parse_plan_sections(text):
        hl = h.lower()
        if any(k in hl for k in _ARCH_GATE_META_SKIP):
            continue
        lead = re.sub(r"\s+", " ", b).strip()[:_ARCH_GATE_CLAIM_CHARS]
        if len(lead) >= 30:
            claims.append(f"[{h}] {lead}")
        if "files" in hl:
            for line in b.splitlines():
                paths = re.findall(r"`([^`]+)`", line)
                purpose = re.sub(r"`[^`]*`", "", line).strip().lstrip("-—– ").strip()
                for tok in paths:
                    if len(tok.strip()) > 3:
                        claims.append(f"[files] {tok.strip()}: {purpose[:150]}")
    out, seen = [], set()
    for c in claims:
        key = c.lower()[:80]
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out[:_ARCH_GATE_MAX_CLAIMS]


def cmd_arch_gate(args: argparse.Namespace) -> int:
    """
    Architectural fit gate: does the plan introduce anything that goes against
    the architecture documented in TECHNICAL-DOCUMENTATION.md? Per plan claim:
    documented noul (does the doc address this area), conflict noul (does the
    documented architecture contradict it), verdict choice (conforms / drifts /
    undocumented). Fail-closed derivation: a high conflict signal downgrades a
    conforms verdict; a low documented signal forces "undocumented" (a
    conventions gap, not a drift accusation — no rubric, no verdict). Drifts
    exit 1; undocumented claims are reported as documentation findings.
    """
    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return EXIT_ERROR
    if sys1 is None:
        print("error: arch-gate requires sys1", file=sys.stderr)
        return EXIT_ERROR

    repo_root = Path(getattr(args, "repo_root", None) or Path.cwd())
    doc_rel = getattr(args, "tech_doc", None) or "TECHNICAL-DOCUMENTATION.md"
    doc_path = repo_root / doc_rel
    if not doc_path.exists():
        print(f"error: technical documentation not found: {doc_path} (pass --tech-doc)", file=sys.stderr)
        return EXIT_ERROR

    claims = _arch_gate_claims(plan_path.read_text())
    if not claims:
        print("error: no architectural claims found in the plan (approach/phases/files sections).", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(None)
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]

    doc_sections = _parse_plan_sections(doc_path.read_text())[:_ARCH_GATE_DOC_SECTIONS]

    heads: list = []
    for k, claim in enumerate(claims):
        heads.append(sys1.make_noul(
            f"Does the technical documentation address the architectural area this plan claim touches? Claim: {claim}",
            id=f"k{k}_documented"))
        heads.append(sys1.make_noul(
            f"Does the documented architecture contradict or advise against this plan claim? Claim: {claim}",
            id=f"k{k}_conflict"))
        heads.append(sys1.make_choice(
            f"Architectural verdict for this plan claim against the documented architecture?",
            list(_ARCH_GATE_VERDICTS),
            id=f"k{k}_verdict",
            descriptions={
                "conforms": "The claim is consistent with the documented architecture and patterns.",
                "drifts": "The documented architecture contradicts this claim or advises against it.",
                "undocumented": "The documentation does not cover this area; no architectural verdict is possible.",
            }))

    parts = [f"Architecture fit check for a plan with {len(claims)} claims (K0..K{len(claims) - 1}) against the technical documentation sections (d<j>.<s>)."]
    for k, c in enumerate(claims):
        parts.append(f"K{k}: {c}")
    for j, (h, b) in enumerate(doc_sections):
        parts.append(f"=== d{j} [{h}] ===\n{b[:_ARCH_GATE_DOC_SECTION_CHARS]}")
    state = "\n\n".join(parts)[: cfg["providers"].get("drex_max_chars", 400_000)]

    task = sys1.types.Task(id="arch_gate", heads=heads, description="Plan vs documented-architecture conformance gate (speculative fan-out)")
    chain = _sys1_chain(provider) if provider != "auto" else sys1.routing.route_decision(task, state, cfg)[0] or _sys1_chain("jev")
    result = sys1.classify(chain, task, state, cfg=cfg, log=False)
    answers: dict = {}
    used_provider = chain[0] if chain else ""
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        if result.answers.get(mapped):
            answers = result.answers[mapped]
            used_provider = mapped
            break
    if not answers:
        print(f"error: no provider answered (chain: {', '.join(chain)})", file=sys.stderr)
        return EXIT_ERROR

    def _noul_p(head_id: str):
        a = answers.get(head_id) or {}
        p = a.get("noul")
        return p if isinstance(p, (int, float)) else None

    print(f"Arch gate: {plan_path.name} vs {doc_rel}  (provider={used_provider}, {result.latency_ms} ms, {len(claims)} claims, {len(doc_sections)} doc sections)")
    drifts, gaps = [], []
    for k, claim in enumerate(claims):
        documented = _noul_p(f"k{k}_documented")
        conflict = _noul_p(f"k{k}_conflict")
        v = (answers.get(f"k{k}_verdict") or {}).get("label") or "undocumented"
        # Fail-closed derivation: no rubric -> undocumented (never accuse
        # without documentation); a strong conflict signal downgrades a
        # conforms verdict. Never upgrades drifts away.
        if isinstance(documented, (int, float)) and documented < 0.4:
            v = "undocumented"
        if isinstance(conflict, (int, float)) and conflict >= 0.6 and v == "conforms":
            v = "drifts"
        d_s = f"{documented:.2f}" if isinstance(documented, (int, float)) else "----"
        c_s = f"{conflict:.2f}" if isinstance(conflict, (int, float)) else "----"
        print(f"    {v.upper():<13} (documented={d_s}, conflict={c_s})  {claim[:95]}")
        if v == "drifts":
            drifts.append(k)
        if v == "undocumented":
            gaps.append(k)

    log_record({
        "op": "arch-gate",
        "plan": str(plan_path),
        "tech_doc": doc_rel,
        "provider": used_provider,
        "task": "arch_gate",
        "input_sha256": _sha16(state),
        "heads": answers,
        "claims": len(claims),
        "claim_texts": [str(c.get("text") if isinstance(c, dict) else c)[:140] for c in claims],
        "drifts": drifts,
        "undocumented": gaps,
        "verdict": "drift" if drifts else ("conventions-gaps" if gaps else "pass"),
        "latency_ms": result.latency_ms,
        "telemetry": result.telemetry.get(used_provider, {}),
    })
    if drifts:
        print(f"  verdict: DRIFT ({len(drifts)} claim(s) conflict with the documented architecture) — human review required")
    elif gaps:
        print(f"  verdict: PASS with {len(gaps)} conventions gap(s) — document these areas to make future gates stricter")
    else:
        print("  verdict: PASS")
    return EXIT_WARN if drifts else EXIT_OK


def cmd_disposition(args: argparse.Namespace) -> int:
    """
    Record the human decision on a negative gate verdict — the HITL contract
    that makes advisorial gates load-bearing:
      fixed       the flagged item was reworked; the gate was right.
      waived      consciously proceeding despite the flag; reason required.
      overridden  the gate was wrong (model error); reason required.
    Waive/override reasons are the calibration signal: a gate with a recurring
    override pattern needs rework, and a low override rate is what earns a
    gate the flip from advisorial to blocking.
    """
    if args.status not in ("fixed", "waived", "overridden"):
        print("error: --status must be fixed | waived | overridden", file=sys.stderr)
        return EXIT_ERROR
    if args.status in ("waived", "overridden") and not (args.reason or "").strip():
        print(f"error: --reason is required for {args.status} dispositions — the reason is the calibration signal.", file=sys.stderr)
        return EXIT_ERROR
    log_record({
        "op": "disposition",
        "gate": args.gate,
        "target": args.target,
        "status": args.status,
        "reason": (args.reason or "").strip(),
        "decided_by": getattr(args, "by", None) or os.environ.get("USER", "user"),
    })
    print(f"disposition recorded: {args.gate} {args.target} -> {args.status}")

    # Close the calibration loop: pair this human verdict with the original
    # prediction so the JSONL gains graded rows without a separate grading
    # session (2026-10-02: production had ZERO feedback rows before manual
    # grading because this step never existed).
    try:
        event = _find_gate_event(args.gate, args.target)
        if event is None:
            print("  (no matching gate event found — no feedback rows written)")
        else:
            rows = _disposition_feedback_rows(args.status, event, args.reason)
            written = 0
            for r in rows:
                if not r["actual"]:
                    continue
                _write_feedback(r["input_sha256"], r["task"], r["provider"],
                                r["actual"], r["note"], r["head_id"])
                written += 1
            print(f"  calibration: {written} feedback row(s) written from the matched event"
                  + ("" if event.get("input_sha256") else " (event lacks input_sha256 — pre-enrichment row)"))
    except Exception as e:
        print(f"  (feedback pairing skipped: {e})")
    return EXIT_OK


def cmd_plan_reconcile(args: argparse.Namespace) -> int:
    """
    Grade a plan's surface artifact against the actual commit sequence (the
    forward-validation half of plan-surface): reconstruct the per-head
    predictions, run the attribution layer over the commits, reconcile
    predicted order/edges vs reality, and write plan_surface_map/plan_deps
    feedback rows. Run after a plan completes; re-runnable.
    """
    if _plansurface is None:
        print("error: plan-reconcile requires sys1 with the plansurface module", file=sys.stderr)
        return EXIT_ERROR
    plan_path = Path(args.plan).expanduser()
    if not plan_path.exists():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return EXIT_ERROR
    artifact_path = (Path(args.artifact).expanduser() if getattr(args, "artifact", None)
                     else SURFACES_DIR / f"{plan_path.stem}.surface.json")
    if not artifact_path.exists():
        print(f"error: no surface artifact at {artifact_path} — run `dev-decisions plan-surface` first", file=sys.stderr)
        return EXIT_ERROR
    artifact = json.loads(artifact_path.read_text())
    meta = artifact.get("meta", {})
    repo_root = Path(args.repo_root).expanduser() if args.repo_root else Path(meta.get("repo_root") or Path.cwd())
    criteria = meta.get("criteria_texts") or _parse_plan_criteria(plan_path.read_text())
    if not criteria:
        print("error: no criteria found (artifact meta or plan checkboxes)", file=sys.stderr)
        return EXIT_ERROR
    cfg = load_config(None)

    # per-head predictions: persisted answers on new artifacts, argmax
    # summaries on pre-2026-10-02 artifacts
    answers = meta.get("answers") or {}
    map_answers = dict(answers.get("map") or {})
    dep_answers = dict(answers.get("deps") or {})
    if not map_answers:
        for node in artifact.get("criteria", []):
            try:
                i = int(node["id"][1:])
            except (ValueError, KeyError, TypeError):
                continue
            mods = node.get("modules") or []
            if mods:
                map_answers[f"c{i}_modules"] = {
                    "label": mods[0]["id"], "confidence": mods[0]["p"],
                    "probabilities": {m["id"]: m["p"] for m in mods}}
            anyp = node.get("touches_any")
            if isinstance(anyp, (int, float)):
                map_answers[f"c{i}_any"] = {"noul": anyp}
    if not dep_answers:
        for key, probs in (meta.get("dep_probs") or {}).items():
            if not isinstance(probs, dict) or not probs:
                continue
            i, j = key.split(",")
            label = max(probs, key=lambda k: probs[k])
            dep_answers[f"p_{i}_{j}"] = {"label": label, "confidence": probs[label],
                                         "probabilities": probs}
    map_res = {"provider": meta.get("provider_map", ""), "answers": map_answers,
               "sha256": meta.get("sha_map") or _sha16(json.dumps(artifact, sort_keys=True))}
    dep_res = {"provider": meta.get("provider_deps", ""), "answers": dep_answers,
               "sha256": meta.get("sha_deps") or "", "sha_by_head": {}}

    # commit window: frontmatter created minus one day (same default as the probe)
    created = None
    m = re.search(r"^created:\s*(\S+)", plan_path.read_text(), re.M)
    if m:
        try:
            from datetime import datetime as _dt, timedelta as _td
            created = _dt.fromisoformat(m.group(1))
        except ValueError:
            created = None
    default_since = ((created - _td(days=1)).date().isoformat()
                     if created else "1970-01-01")
    since = args.since or default_since

    commits = _plansurface.load_commits(repo_root, since=since, max_commits=args.max_commits)
    print(f"Plan reconcile: {plan_path.name}  ({len(criteria)} criteria, {len(commits)} commits since {since})")
    attributed: dict = {}
    if args.attribute_provider and commits:
        print(f"  attribution layer ({args.attribute_provider}) ...")
        attr = _plansurface.run_attribution(commits, criteria, provider=args.attribute_provider, cfg=cfg)
        for k, c in enumerate(commits):
            lab = (attr["answers"].get(f"a{k}_crit") or {}).get("label")
            if lab and lab != "none":
                attributed[c["sha"]] = lab

    graded = _plansurface.grade_and_record(artifact, commits, map_res, dep_res,
                                           attributed=attributed or None, cfg=cfg)
    rec = graded["reconcile"]
    at = rec["at_threshold"]
    print(f"  predicted order: {' -> '.join(rec['predicted_order']) or '(none)'}")
    print(f"  actual order:    {' -> '.join(rec['actual_order']) or '(none)'}")
    print(f"  tau={rec['tau']}  direction_precision={at['direction_precision']}  "
          f"adjacent_coverage={at['adjacent_coverage']}  "
          f"unimplemented={', '.join(rec['criteria_unimplemented']) or '(none)'}")
    print(f"  feedback rows: {graded['feedback_rows']}")

    log_record({
        "op": "plan-reconcile",
        "plan": str(plan_path),
        "repo": str(repo_root),
        "provider": f"map={meta.get('provider_map')},deps={meta.get('provider_deps')}",
        "task": "plan_surface_map+plan_deps",
        "criteria": len(criteria),
        "commits": len(commits),
        "tau": rec["tau"],
        "direction_precision": at["direction_precision"],
        "adjacent_coverage": at["adjacent_coverage"],
        "unimplemented": rec["criteria_unimplemented"],
        "feedback_rows": graded["feedback_rows"],
        "verdict": "ok",
    })
    return EXIT_OK


def cmd_calibration(args: argparse.Namespace) -> int:
    """
    Which heads have enough graded rows to fit confidence floors? Reads the
    dev-decisions feedback store AND the sys1 store (graded rows from manual
    grading sessions land there), joins per (task, head, provider), and
    reports count, confidence span, binned accuracy, and a qualifies verdict
    (>= --min-rows spanning >= --min-span). The 'when to fit' answer as a
    command, not a judgment call.
    """
    feedback_rows: list = []

    def _absorb(path):
        if not path.exists():
            return 0
        n = 0
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("op") == "feedback" or ("actual" in r and "predicted" in r) or                     ("label" in r and "input_sha256" in r):
                predicted = r.get("predicted")
                if predicted is None:
                    predicted = r.get("label")
                feedback_rows.append({
                    "task": r.get("task"), "provider": r.get("provider"),
                    "head_id": r.get("head_id"),
                    "predicted": predicted,
                    "confidence": r.get("predicted_confidence"),
                    "actual": r.get("actual"),
                })
                n += 1
        return n

    n_dd = _absorb(LOG_DIR / "feedback" / "feedback.jsonl")
    n_sys1 = 0
    try:
        if _plansurface is not None:
            sys1_cfg = sys1.load_config()
            sys1_log = Path(sys1_cfg["classify"].get("log_dir")
                            or Path.home() / ".local/share/sys1/logs")
            n_sys1 = sum(_absorb(f) for f in sorted(sys1_log.glob("20*/*/*/events.jsonl")))
    except Exception as e:
        print(f"(sys1 store unreadable: {e})", file=sys.stderr)

    groups: dict = {}
    for r in feedback_rows:
        key = (str(r["task"]), str(r.get("head_id") or "-"), str(r.get("provider") or "-"))
        groups.setdefault(key, []).append(r)

    report = []
    for (task, head_id, provider), rows in sorted(groups.items()):
        graded = [r for r in rows if r["predicted"] is not None and r["actual"] is not None]
        n = len(graded)
        correct = sum(1 for r in graded if str(r["predicted"]) == str(r["actual"]))
        confs = [float(r["confidence"]) for r in rows if isinstance(r.get("confidence"), (int, float))]
        span = round(max(confs) - min(confs), 3) if confs else 0.0
        bins = [0, 0] * 2  # [count, correct] per quartile
        curve = []
        for lo in (0.0, 0.25, 0.5, 0.75):
            inbin = [r for r in graded if isinstance(r.get("confidence"), (int, float))
                     and lo <= r["confidence"] < lo + 0.25]
            ok = sum(1 for r in inbin if str(r["predicted"]) == str(r["actual"]))
            curve.append({"bin": [lo, lo + 0.25], "n": len(inbin), "accuracy": round(ok / len(inbin), 3) if inbin else None})
        qualifies = n >= args.min_rows and span >= args.min_span
        report.append({"task": task, "head": head_id, "provider": provider,
                       "n": n, "accuracy": round(correct / n, 3) if n else None,
                       "span": span, "curve": curve, "qualifies_for_fit": qualifies})

    if args.format == "json":
        print(json.dumps({"sources": {"dev-decisions": n_dd, "sys1": n_sys1}, "groups": report}, indent=2))
        return EXIT_OK

    print(f"Calibration: {n_dd} rows from dev-decisions, {n_sys1} from the sys1 store")
    print(f"{'task':<22} {'head':<24} {'provider':<12} {'n':>4} {'acc':>6} {'span':>5}  fit?")
    for g in report:
        print(f"{g['task']:<22} {g['head']:<24} {g['provider']:<12} {g['n']:>4} "
              f"{str(g['accuracy']):>6} {g['span']:>5}  {'YES' if g['qualifies_for_fit'] else ''}")
    return EXIT_OK


def cmd_triage_issues(args: argparse.Namespace) -> int:
    """Batch-classify issues and apply labels."""
    repo = getattr(args, "repo", None)
    state = getattr(args, "state", "open")
    limit = getattr(args, "limit", 10)
    dry_run = getattr(args, "dry_run", True)

    if not shutil.which("gh"):
        print("error: gh CLI required (https://cli.github.com/)", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(None)
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]
    task = "issue_triage"
    if sys1 is None:
        heads = get_task_heads(task, provider.split("+")[0])

    # Fetch issues
    gh_args = ["issue", "list", "--state", state, "--limit", str(limit), "--json", "number,title,body,labels"]
    if repo:
        gh_args += ["-R", repo]
    try:
        issues_json = _call_gh(gh_args)
    except RuntimeError as e:
        print(f"error: failed to list issues: {e}", file=sys.stderr)
        return EXIT_ERROR

    issues = json.loads(issues_json) if issues_json else []
    if not issues:
        print(f"No {state} issues found.")
        return EXIT_OK

    print(f"Triage: {len(issues)} issues ({state})\n")
    print(f"  {'#':<6} {'kind':<12} {'priority':<10} {'labels':<30} title")
    print(f"  {'─'*6} {'─'*12} {'─'*10} {'─'*30} {'─'*40}")

    items: list[dict] = []
    for issue in issues:
        num = issue["number"]
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        text = f"{title}\n\n{body}"[:DEFAULT_MAX_DIFF_CHARS]

        item_telemetry: dict = {}
        try:
            if sys1 is not None:
                single = _sys1_classify_single(provider, task, text, cfg)
                parsed = single.get("answers", {})
                item_telemetry = single.get("telemetry", {})
            elif provider in ("decide", "both"):
                result = _call_openai_compatible(
                    cfg["providers"]["decide_api_url"],
                    _env_key("decide"),
                    cfg["providers"]["decide_model"],
                    [
                        {"role": "system", "content": "You are an issue triager. Answer only with the requested labels."},
                        {"role": "user", "content": f"Classify this issue.\n\n{text}"},
                    ],
                    schema={"classifications": heads},
                    timeout=cfg["providers"]["request_timeout_seconds"],
                    max_retries=cfg["providers"]["max_retries"],
                    backoff=cfg["providers"]["retry_backoff_seconds"],
                    telemetry=item_telemetry,
                )
                parsed = _parse_decide_response(result)
            elif provider == "jev":
                jev_telemetry: dict = {}
                questions = get_task_heads(task, "jev")
                payload_questions = {q["id"]: {"type": q["type"], "instructions": q["instructions"], "criteria": q.get("criteria")} for q in questions}
                body = json.dumps({"state": {"text": text[:DEFAULT_MAX_DIFF_CHARS], "task": task}, "questions": payload_questions, "model": cfg["providers"]["jev_model"]})
                resp = _call_jev_raw(body, _env_key("jev"), cfg, telemetry=jev_telemetry)
                parsed = _parse_jev_response(resp, [q["id"] for q in questions])
                item_telemetry = jev_telemetry
            elif provider == "modernbert":
                modernbert_telemetry: dict = {}
                parsed = _call_modernbert_provider(text, heads, cfg, telemetry=modernbert_telemetry)
                item_telemetry = modernbert_telemetry
            else:
                local_telemetry: dict = {}
                parsed = _call_local_provider(text, heads, cfg, telemetry=local_telemetry)
                item_telemetry = local_telemetry
        except Exception as e:
            print(f"  {num:<6} error: {e}")
            continue

        kind = ""
        priority = ""
        labels_to_apply = []
        for key, val in parsed.items():
            if isinstance(val, dict):
                label = val.get("label")
                if label:
                    if isinstance(label, list):
                        labels_to_apply.extend(label)
                    else:
                        labels_to_apply.append(label)
                    # infer kind/priority from key (sys1 head IDs are short: kind/priority)
                    kl = key.lower()
                    if "kind" in kl:
                        kind = label if isinstance(label, str) else str(label)
                    elif "priority" in kl:
                        priority = label if isinstance(label, str) else str(label)

        labels_to_apply = sorted(set(labels_to_apply))
        print(f"  {num:<6} {kind:<12} {priority:<10} {', '.join(labels_to_apply):<30} {title[:40]}")

        if not dry_run and labels_to_apply:
            for label in labels_to_apply:
                try:
                    _call_gh(["issue", "edit", str(num), "--add-label", label])
                except RuntimeError as e:
                    print(f"    ✗ failed to apply {label}: {e}", file=sys.stderr)

        items.append({
            "num": num,
            "title": title,
            "kind": kind,
            "priority": priority,
            "labels": labels_to_apply,
            "telemetry": item_telemetry,
        })

    log_record({
        "op": "triage-issues",
        "repo": repo,
        "count": len(issues),
        "task": task,
        "provider": provider,
        "dry_run": dry_run,
        "items": items,
    })
    return EXIT_OK


def cmd_changelog(args: argparse.Namespace) -> int:
    """Generate Keep-a-Changelog markdown from commits since a ref."""
    since = args.since
    write = getattr(args, "write", False)
    provider = getattr(args, "provider", None) or load_config(None)["classify"]["provider"]

    repo = get_repo_root()
    if not repo:
        print("error: not in a git repository", file=sys.stderr)
        return EXIT_ERROR

    # Collect commits since ref
    try:
        log_out = subprocess.run(
            ["git", "log", "--oneline", "--no-merges", f"{since}..HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"error: git log failed: {e.stderr}", file=sys.stderr)
        return EXIT_ERROR

    if not log_out:
        print(f"No commits since {since}.")
        return EXIT_OK

    commits = []
    for line in log_out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append({"sha": parts[0], "subject": parts[1]})

    print(f"Changelog: {len(commits)} commits since {since}\n")

    # Group headers
    sections = {"Added": [], "Changed": [], "Fixed": [], "Removed": [], "Security": []}
    cfg = load_config(repo)
    if sys1 is None:
        heads = get_task_heads("change", provider.split("+")[0])
    last_commit_telemetry: dict = {}

    for c in commits:
        sha = c["sha"]
        subject = c["subject"]

        # Get diff for classification
        try:
            diff = subprocess.run(
                ["git", "show", "--stat", "--format=", sha],
                cwd=repo, capture_output=True, text=True, check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            diff = ""

        if not diff:
            sections["Changed"].append(subject)
            continue

        commit_telemetry: dict = {}
        try:
            if sys1 is not None:
                single = _sys1_classify_single(provider, "change", diff, cfg)
                parsed = single.get("answers", {})
                commit_telemetry = single.get("telemetry", {})
            elif provider in ("decide", "both"):
                result = _call_openai_compatible(
                    cfg["providers"]["decide_api_url"],
                    _env_key("decide"),
                    cfg["providers"]["decide_model"],
                    [
                        {"role": "system", "content": "You are a changelog classifier. Answer only with the requested labels."},
                        {"role": "user", "content": f"Classify this commit for a changelog.\n\nSubject: {subject}\n\nDiff:\n{diff}"},
                    ],
                    schema={"classifications": heads},
                    timeout=cfg["providers"]["request_timeout_seconds"],
                    max_retries=cfg["providers"]["max_retries"],
                    backoff=cfg["providers"]["retry_backoff_seconds"],
                    telemetry=commit_telemetry,
                )
                parsed = _parse_decide_response(result)
            elif provider == "jev":
                jev_telemetry: dict = {}
                questions = get_task_heads("change", "jev")
                payload_questions = {q["id"]: {"type": q["type"], "instructions": q["instructions"], "criteria": q.get("criteria")} for q in questions}
                body = json.dumps({"state": {"subject": subject, "diff": diff[:DEFAULT_MAX_DIFF_CHARS]}, "questions": payload_questions, "model": cfg["providers"]["jev_model"]})
                resp = _call_jev_raw(body, _env_key("jev"), cfg, telemetry=jev_telemetry)
                parsed = _parse_jev_response(resp, [q["id"] for q in questions])
                commit_telemetry = jev_telemetry
            elif provider == "modernbert":
                modernbert_telemetry: dict = {}
                parsed = _call_modernbert_provider(diff, heads, cfg, telemetry=modernbert_telemetry)
                commit_telemetry = modernbert_telemetry
            else:
                local_telemetry: dict = {}
                parsed = _call_local_provider(diff, heads, cfg, telemetry=local_telemetry)
                commit_telemetry = local_telemetry
        except Exception as e:
            sections["Changed"].append(subject)
            continue

        # Map diff_type to section (sys1 head ID = "diff_type"; legacy = task[:40])
        diff_type = "change"
        for key, val in parsed.items():
            if isinstance(val, dict) and "type" in key.lower():
                label = val.get("label")
                if label:
                    diff_type = label if isinstance(label, str) else str(label)
                    break

        # Heuristic mapping
        if diff_type in ("feat", "feature"):
            sections["Added"].append(subject)
        elif diff_type in ("fix", "bug"):
            sections["Fixed"].append(subject)
        elif diff_type in ("refactor", "perf", "dep", "deps"):
            sections["Changed"].append(subject)
        elif diff_type in ("remove", "removed", "del"):
            sections["Removed"].append(subject)
        elif diff_type in ("sec", "security"):
            sections["Security"].append(subject)
        else:
            sections["Changed"].append(subject)

        last_commit_telemetry = commit_telemetry

    # Render markdown
    lines = [f"## [{args.next_version or 'Unreleased'}]\n"]
    for section, items in sections.items():
        if items:
            lines.append(f"### {section}\n")
            for item in items:
                lines.append(f"- {item}")
            lines.append("")

    md = "\n".join(lines).strip() + "\n"
    print(md)

    if write:
        changelog_path = repo / "CHANGELOG.md"
        existing = changelog_path.read_text() if changelog_path.exists() else ""
        # Prepend new section after first heading if present
        if existing.startswith("#"):
            first_heading_end = existing.find("\n## ")
            if first_heading_end != -1:
                new_content = existing[:first_heading_end] + "\n\n" + md + existing[first_heading_end:]
            else:
                new_content = existing + "\n\n" + md
        else:
            new_content = md + existing
        changelog_path.write_text(new_content)
        print(f"Written to {changelog_path}")

    log_record({
        "op": "changelog",
        "since": since,
        "commits": len(commits),
        "provider": provider,
        "write": write,
        "repo": repo_name(repo),
        "commit_telemetry": commit_telemetry if 'commit_telemetry' in dir() else {},
    })
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    repo = get_repo_root()
    cfg = load_config(repo)

    def mask(k):
        return "set" if k else "not set"

    print("dev-decisions config\n" + "=" * 40)
    print(f"  FASTINO_API_KEY:   {mask(os.environ.get('FASTINO_API_KEY'))}")
    print(f"  TYPESAFE_API_KEY:  {mask(os.environ.get('TYPESAFE_API_KEY'))}")
    print()
    for section, values in cfg.items():
        print(f"  [{section}]")
        for k, v in values.items():
            print(f"    {k} = {v}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    import shutil
    issues = []

    # python version
    v = sys.version_info
    print(f"python: {v.major}.{v.minor}.{v.micro}")
    if v < (3, 10):
        issues.append("Python 3.10+ recommended")

    # git
    git_path = shutil.which("git")
    print(f"git:    {'found at ' + git_path if git_path else 'NOT FOUND'}")

    # env keys
    for name in ("FASTINO_API_KEY", "TYPESAFE_API_KEY"):
        val = os.environ.get(name)
        print(f"{name}: {'set' if val else 'not set'}")

    # uv (optional)
    uv_path = shutil.which("uv")
    print(f"uv:     {'found' if uv_path else 'not found'} (optional, for local GLiNER)")

    # sys1 library (preferred provider implementation)
    if sys1 is not None:
        try:
            sys1_path = Path(sys1.__file__).parent
        except Exception:
            sys1_path = Path("?")
        try:
            sys1_version = sys1.__version__
        except Exception:
            sys1_version = "?"
        print(f"sys1:   v{sys1_version} at {sys1_path}")
        try:
            health = sys1.health_report(load_config(repo_root=None))
            avail = [pid for pid, info in health.items() if info.get("available")]
            print(f"  providers available: {', '.join(avail) or 'none'}")
        except Exception as e:
            print(f"  sys1 health check failed: {e}")
    else:
        print("sys1:   not importable — using inline provider code path")
        print("        (install: pip install -e ~/Projects/sys1, or set DEV_DECISIONS_SYS1_PATH)")

    # config
    if CONFIG_FILE.exists():
        print(f"config: {CONFIG_FILE} ✓")
    else:
        print(f"config: {CONFIG_FILE} (missing — using defaults)")

    # hooks
    repo = get_repo_root()
    if repo:
        hooks = repo / ".git" / "hooks"
        for h in ("pre-commit", "pre-push"):
            f = hooks / h
            marker = "# dev-decisions hook"
            status = "installed" if f.exists() and marker in f.read_text(errors="replace") else "not installed"
            print(f"hook {h}: {status}")

    # log dir
    try:
        _log_dir().mkdir(parents=True, exist_ok=True)
        print(f"log dir: writable ✓")
    except Exception as e:
        issues.append(f"log dir unwritable: {e}")
        print(f"log dir: UNWRITABLE ({e})")

    if issues:
        print("\nissues:")
        for i in issues:
            print(f"  ✗ {i}")
        return EXIT_WARN
    print("\nAll checks passed.")
    return EXIT_OK


def cmd_feedback(args: argparse.Namespace) -> int:
    """
    Record human feedback for a previous classification.
    Appends to LOG_DIR/feedback.jsonl with ts, input_sha256, task,
    provider (optional), label (ground truth), and note (optional).
    Dashboard joins on (input_sha256, task) to compute calibration curves.
    """
    input_sha256 = getattr(args, "input_sha256", None)
    if not input_sha256:
        print("error: input_sha256 is required", file=sys.stderr)
        return EXIT_ERROR

    task = getattr(args, "task", None)
    if not task:
        print("error: --task is required", file=sys.stderr)
        return EXIT_ERROR

    label = getattr(args, "label", None)
    if not label:
        print("error: --label is required", file=sys.stderr)
        return EXIT_ERROR

    provider = getattr(args, "provider", None)
    note = getattr(args, "note", None)
    head_id = getattr(args, "head_id", None)

    try:
        _write_feedback(input_sha256, task, provider, label, note, head_id)
        print(f"Logged feedback for {input_sha256} (task={task}, label={label})")
        return EXIT_OK
    except Exception as e:
        print(f"error: failed to write feedback: {e}", file=sys.stderr)
        return EXIT_ERROR


def _write_feedback(input_sha256: str, task: str, provider: str | None, label: str,
                    note: str | None = None, head_id: str | None = None) -> None:
    """One graded-outcome row in feedback.jsonl (head_id additive; the
    dashboard join reads only known keys, so extra keys are safe)."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "input_sha256": input_sha256,
        "task": task,
        "provider": provider,
        "label": label,
        "note": note,
    }
    if head_id:
        record["head_id"] = head_id
    feedback_dir = LOG_DIR / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    feedback_path = feedback_dir / "feedback.jsonl"
    with open(feedback_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


_GATE_OP_TARGET_FIELD = {
    "plan-gate": ("plan-gate", "plan"),
    "arch-gate": ("arch-gate", "plan"),
    "docs-gate": ("docs-gate", "plan"),
    "evidence-gate": ("evidence-gate", "plan"),
    "pr-gate": ("pr-gate", "repo"),
    "zcode-gate": ("zcode-gate", "command"),
}


def _find_gate_event(gate: str, target: str, days: int = 30) -> dict | None:
    """Most recent event row for this gate whose target field matches the
    disposition target (substring either way). Newest file first, bounded
    window — dispositions are written close to the verdict they close."""
    spec = _GATE_OP_TARGET_FIELD.get(gate)
    if not spec:
        return None
    op, field = spec
    cutoff = time.time() - days * 86400
    for f in sorted(LOG_DIR.glob("20*/*/*/events.jsonl"), reverse=True):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            for line in reversed(f.read_text().splitlines()):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("op") != op:
                    continue
                val = str(row.get(field) or "")
                tgt = str(target)
                if val and (val in tgt or tgt in val):
                    return row
        except Exception:
            continue
    return None


def _disposition_feedback_rows(status: str, row: dict, reason: str | None) -> list:
    """
    Map a disposition onto per-head feedback rows (the HITL calibration loop,
    2026-10-02 grading session): fixed/waived mean the gate judged right
    (actual = predicted); overridden means the model was wrong (actual =
    inverse for yes/no heads, None for choice heads where inversion is
    undefined). Requires the event row to carry input_sha256 + heads — the
    enriched rows written since 2026-10-02 do; older rows are skipped.
    """
    out: list = []
    sha = row.get("input_sha256")
    if not sha:
        return out
    heads = row.get("heads") if isinstance(row.get("heads"), dict) else {}
    provider = row.get("provider")
    flat = heads.get(provider) if isinstance(heads.get(provider), dict) else heads
    if not isinstance(flat, dict):
        # multi-provider chains log "glide+drex" style — split and take the
        # first resolvable
        for part in str(provider or "").split("+"):
            if isinstance(heads.get(part), dict):
                flat = heads[part]
                break
    if not isinstance(flat, dict):
        return out
    task = row.get("task") or row.get("op")
    note = f"disposition {status}: {(reason or '').strip()[:140]}"
    for head_id, a in flat.items():
        if not isinstance(a, dict) or str(head_id).startswith("_"):
            continue
        predicted = a.get("label")
        conf = a.get("confidence")
        if predicted is None and isinstance(a.get("noul"), (int, float)):
            predicted = "yes" if a["noul"] >= 0.5 else "no"
            conf = round(float(a["noul"]), 4)
        if predicted is None:
            continue
        if status == "overridden":
            actual = ("no" if predicted == "yes" else "yes") if predicted in ("yes", "no") else None
        else:
            actual = predicted
        out.append({"input_sha256": sha, "task": task, "provider": provider,
                    "head_id": head_id, "predicted": predicted,
                    "predicted_confidence": conf, "actual": actual, "note": note})
    return out


# ── dashboard ─────────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dev-decisions dashboard</title>
<style>
  :root {
    --bg: #0b0f19;
    --panel: #111827;
    --border: #1f2937;
    --text: #e5e7eb;
    --muted: #9ca3af;
    --accent: #60a5fa;
    --danger: #f87171;
    --warn: #fbbf24;
    --success: #34d399;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    padding: 18px 24px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, rgba(17,24,39,.9), rgba(17,24,39,.6));
  }
  header h1 {
    margin: 0 0 6px 0;
    font-size: 18px;
    letter-spacing: .2px;
  }
  header p { margin: 0; color: var(--muted); font-size: 12px; }
  main {
    padding: 18px;
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 16px;
  }
  .panel {
    grid-column: span 12;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
  }
  .panel h2 {
    margin: 0 0 10px 0;
    font-size: 13px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .6px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    font-family: var(--mono);
  }
  th, td { padding: 7px 9px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  tr:last-child td { border-bottom: none; }
  .badge {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 999px;
    font-size: 11px;
    background: #1f2937;
    border: 1px solid #374151;
  }
  .ok { color: var(--success); }
  .warn { color: var(--warn); }
  .err { color: var(--danger); }
  .muted { color: var(--muted); }
  .bar { height: 8px; border-radius: 4px; background: #1f2937; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: var(--accent); }
  code { font-family: var(--mono); font-size: 11px; color: #c7d2fe; background: #0b1220; padding: 2px 6px; border-radius: 4px; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .col { flex: 1 1 280px; min-width: 260px; }
  @media (max-width: 980px) {
    main { grid-template-columns: 1fr; }
    .panel { grid-column: span 1; }
  }
</style>
</head>
<body>
<header>
  <h1>dev-decisions</h1>
  <p>provider telemetry • calibration • modernbert eval</p>
</header>
<main>
  <section class="panel">
    <h2>provider health</h2>
    <div id="summary">loading…</div>
  </section>

  <section class="panel">
    <h2>confidence histograms</h2>
    <div id="confidence">loading…</div>
  </section>

  <section class="panel">
    <h2>modernbert signals</h2>
    <div id="modernbert">loading…</div>
  </section>

  <section class="panel">
    <h2>cross-provider agreement</h2>
    <div id="agreement">loading…</div>
  </section>

  <section class="panel">
    <h2>calibration (human feedback)</h2>
    <div id="calibration">loading…</div>
  </section>
</main>

<script>
function el(tag, cls, text){ const e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function num(v){ const n=Number(v); return Number.isFinite(n)?n:null; }
function fmt(n){ if(n===null) return '—'; if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1e3) return (n/1e3).toFixed(1)+'k'; return String(n); }
function pct(n){ if(n===null) return '—'; return (n*100).toFixed(1)+'%'; }
function bar(pct){ const d=document.createElement('div'); d.className='bar'; const i=document.createElement('i'); i.style.width=pct; d.appendChild(i); return d; }

async function api(path){
  const r=await fetch(path); if(!r.ok) throw new Error(r.status+' '+r.statusText); return r.json();
}

function renderSummary(data){
  const wrap=document.getElementById('summary');
  wrap.innerHTML='';
  if(!data.providers||!data.providers.length){ wrap.textContent='no data'; return; }
  const row=document.createElement('div'); row.className='row';
  for(const p of data.providers){
    const col=document.createElement('div'); col.className='col panel';
    col.style.background='#0b1220';
    col.style.border='1px solid #1f2937';
    col.innerHTML=`<h2 style="margin-top:0"><code>${p.name}</code> <span class="badge">${fmt(p.calls)} calls</span></h2>`;
    const stats = document.createElement('div');
    stats.style.cssText = 'font-size:12px;margin-bottom:10px;';
    stats.innerHTML = `err <code>${pct(p.error_rate)}</code> · p50 <code>${p.latency_p50?p.latency_p50.toFixed(0)+'ms':'—'}</code> · p95 <code>${p.latency_p95?p.latency_p95.toFixed(0)+'ms':'—'}</code> · null <code>${pct(p.null_rate)}</code>`;
    col.appendChild(stats);
    if(p.latency_bins && p.latency_bins.length){
      const h = document.createElement('div');
      h.innerHTML='<div style="font-size:11px;color:var(--muted);margin-bottom:6px">latency distribution</div>';
      const max = Math.max(...p.latency_bins.map(b=>b.count), 1);
      for(const b of p.latency_bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/max)*100));
        h.appendChild(line);
      }
      col.appendChild(h);
    }
    row.appendChild(col);
  }
  wrap.appendChild(row);
}

function renderConfidence(data){
  const wrap=document.getElementById('confidence');
  wrap.innerHTML='';
  if(!data.providers||!data.providers.length){ wrap.textContent='no data'; return; }
  const row=document.createElement('div'); row.className='row';
  for(const p of data.providers){
    const col=document.createElement('div'); col.className='col panel';
    col.style.background='#0b1220';
    col.style.border='1px solid #1f2937';
    col.innerHTML=`<h2 style="margin-top:0"><code>${p.name}</code> <span class="badge">${p.calls} calls</span></h2>`;
    const bins = (p.fine_bins && p.fine_bins.length) ? p.fine_bins : p.bins;
    const label = (p.fine_bins && p.fine_bins.length) ? 'confidence (fine)' : 'confidence';
    const title = document.createElement('div');
    title.style.cssText = 'font-size:11px;color:var(--muted);margin-bottom:8px;';
    title.textContent = label;
    col.appendChild(title);
    if(!bins||!bins.length){ col.innerHTML+='<div class="muted">no confidence data</div>'; }
    else {
      const max=Math.max(...bins.map(b=>b.count));
      for(const b of bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/Math.max(max,1))*100));
        col.appendChild(line);
      }
    }
    row.appendChild(col);
  }
  wrap.appendChild(row);
}

function renderModernbert(data){
  const wrap=document.getElementById('modernbert');
  wrap.innerHTML='';
  if(!data.tasks||!data.tasks.length){ wrap.innerHTML='<div class="muted">no modernbert data yet</div>'; return; }
  for(const t of data.tasks){
    const section = document.createElement('div');
    section.className='panel';
    section.style.marginBottom='12px';
    section.innerHTML=`<h2 style="margin-top:0"><code>${t.name}</code> <span class="badge">${fmt(t.calls)} calls</span></h2>`;
    const meta = document.createElement('div');
    meta.style.cssText = 'font-size:12px;margin-bottom:10px;';
    meta.innerHTML = `avg top2-gap <code>${t.avg_top2===null?'—':t.avg_top2.toFixed(3)}</code> · embedding norm <code>${t.avg_norm===null?'—':t.avg_norm.toFixed(2)}</code>`;
    section.appendChild(meta);
    if(t.top2_bins && t.top2_bins.length){
      const h = document.createElement('div');
      h.innerHTML='<div style="font-size:11px;color:var(--muted);margin-bottom:6px">top2-gap spread</div>';
      const max = Math.max(...t.top2_bins.map(b=>b.count), 1);
      for(const b of t.top2_bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/max)*100));
        h.appendChild(line);
      }
      section.appendChild(h);
    }
    if(t.norm_bins && t.norm_bins.length){
      const h = document.createElement('div');
      h.innerHTML='<div style="font-size:11px;color:var(--muted);margin:10px 0 6px">embedding-norm spread</div>';
      const max = Math.max(...t.norm_bins.map(b=>b.count), 1);
      for(const b of t.norm_bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/max)*100));
        h.appendChild(line);
      }
      section.appendChild(h);
    }
    const top=(t.top_labels||[]).map(x=>`${x.label}(${x.count})`).join(', ') || '—';
    const labels = document.createElement('div');
    labels.style.cssText = 'font-size:12px;margin-top:8px;';
    labels.innerHTML = `<span class="muted">top labels:</span> ${top}`;
    section.appendChild(labels);
    wrap.appendChild(section);
  }
}

function renderAgreement(data){
  const wrap=document.getElementById('agreement');
  wrap.innerHTML='';
  if(!data.pairs||!data.pairs.length){ wrap.innerHTML='<div class="muted">no cross-provider data yet</div>'; return; }
  const tbl=document.createElement('table');
  tbl.innerHTML=`<thead><tr><th>pair</th><th>compared</th><th>agree</th><th>rate</th></tr></thead>`;
  const body=document.createElement('tbody');
  for(const p of data.pairs){
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td><code>${p.pair}</code></td>
      <td>${fmt(p.compared)}</td>
      <td>${fmt(p.agree)}</td>
      <td>${pct(p.rate)}</td>
    `;
    body.appendChild(tr);
  }
  tbl.appendChild(body);
  wrap.appendChild(tbl);
}

function renderCalibration(data){
  const wrap=document.getElementById('calibration');
  wrap.innerHTML='';
  if(!data.bins||!data.bins.length){ wrap.innerHTML='<div class="muted">no feedback yet. use <code>dev-decisions feedback</code></div>'; return; }
  const tbl=document.createElement('table');
  tbl.innerHTML=`<thead><tr><th>provider</th><th>conf bin</th><th>samples</th><th>correct</th><th>accuracy</th></tr></thead>`;
  const body=document.createElement('tbody');
  for(const b of data.bins){
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td><code>${b.provider}</code></td>
      <td>${b.bin}</td>
      <td>${fmt(b.samples)}</td>
      <td>${fmt(b.correct)}</td>
      <td class="${(b.accuracy||0)>=0.8?'ok':(b.accuracy||0)>=0.6?'warn':'err'}">${pct(b.accuracy)}</td>
    `;
    body.appendChild(tr);
  }
  tbl.appendChild(body);
  wrap.appendChild(tbl);
}

async function init(){
  try {
    const [summary, confidence, modernbert, agreement, calibration] = await Promise.all([
      api('/api/summary?days=7'),
      api('/api/confidence?days=7'),
      api('/api/modernbert?days=7'),
      api('/api/agreement?days=7'),
      api('/api/calibration?days=7'),
    ]);
    renderSummary(summary);
    renderConfidence(confidence);
    renderModernbert(modernbert);
    renderAgreement(agreement);
    renderCalibration(calibration);
  } catch (e) {
    document.body.innerHTML='<main class="panel"><h2>dashboard error</h2><pre>'+e+'</pre></main>';
  }
}
init();
</script>
</body>
</html>
"""


class _DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, days: int = 7, base: Path = LOG_DIR, **kwargs):
        self._days = days
        self._base = base
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass

    def _read_jsonl(self, relpath: str):
        path = self._base / relpath
        if not path.exists():
            return []
        records = []
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _filter_days(self, records):
        cutoff = datetime.now(timezone.utc).timestamp() - (self._days * 86400)
        out = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.get("ts", "")).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                out.append(r)
        return out

    def _json(self, payload, code=200):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _today_events_path(self) -> Path:
        """Return the expected events.jsonl path for today (UTC)."""
        return self._base / datetime.now(timezone.utc).strftime("%Y/%m/%d") / "events.jsonl"

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_DASHBOARD_HTML.encode())))
            self.end_headers()
            self.wfile.write(_DASHBOARD_HTML.encode())
            return
        if self.path.startswith("/api/summary"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            # fallback: read all dated dirs
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            providers = {}
            for r in records:
                for prov, tel in (r.get("telemetry") or {}).items():
                    bucket = providers.setdefault(prov, {"calls": 0, "errors": 0, "latencies": [], "nulls": 0, "nouls": 0, "structured": 0})
                    bucket["calls"] += 1
                    if tel.get("error_kind") or tel.get("http_status", 200) >= 400:
                        bucket["errors"] += 1
                    if tel.get("latency_ms") is not None:
                        bucket["latencies"].append(tel["latency_ms"])
                    if tel.get("null_label_count"):
                        bucket["nulls"] += tel["null_label_count"]
                    if tel.get("noul_count"):
                        bucket["nouls"] += tel["noul_count"]
                    if tel.get("structured_ok"):
                        bucket["structured"] += 1
            provider_list = []
            for name, b in sorted(providers.items()):
                latencies = sorted(b["latencies"])
                p50 = latencies[len(latencies)//2] if latencies else None
                p95 = latencies[int(len(latencies)*0.95)] if latencies else None
                # latency histogram bins: 0-500ms, 500-1000ms, 1-2s, 2-5s, 5-10s, 10s+
                lat_bins = [{"bin": "<500ms", "count": 0}, {"bin": "0.5-1s", "count": 0}, {"bin": "1-2s", "count": 0}, {"bin": "2-5s", "count": 0}, {"bin": "5-10s", "count": 0}, {"bin": ">10s", "count": 0}]
                for lat in latencies:
                    if lat < 500: lat_bins[0]["count"] += 1
                    elif lat < 1000: lat_bins[1]["count"] += 1
                    elif lat < 2000: lat_bins[2]["count"] += 1
                    elif lat < 5000: lat_bins[3]["count"] += 1
                    elif lat < 10000: lat_bins[4]["count"] += 1
                    else: lat_bins[5]["count"] += 1
                provider_list.append({
                    "name": name,
                    "calls": b["calls"],
                    "error_rate": b["errors"] / max(b["calls"], 1),
                    "latency_p50": p50,
                    "latency_p95": p95,
                    "latency_bins": lat_bins,
                    "null_rate": b["nulls"] / max(b["calls"]*2, 1),  # rough: 2 heads per call
                    "noul_rate": b["nouls"] / max(b["calls"]*2, 1),
                    "structured_rate": b["structured"] / max(b["calls"], 1),
                })
            self._json({"providers": provider_list})
            return
        if self.path.startswith("/api/confidence"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            providers = {}
            for r in records:
                for prov, heads in (r.get("heads") or {}).items():
                    bucket = providers.setdefault(prov, [])
                    for h in heads.values():
                        if isinstance(h, dict) and h.get("confidence") is not None:
                            bucket.append(h["confidence"])
            provider_list = []
            for name, confs in sorted(providers.items()):
                bins = [{"bin": "<0.5", "count": 0}, {"bin": "0.5-0.6", "count": 0}, {"bin": "0.6-0.7", "count": 0}, {"bin": "0.7-0.8", "count": 0}, {"bin": "0.8-0.9", "count": 0}, {"bin": "0.9-1.0", "count": 0}]
                fine = [{"bin": f"{(i*0.05):.2f}-{((i+1)*0.05):.2f}", "count": 0} for i in range(20)]
                for c in confs:
                    if c < 0.5: bins[0]["count"] += 1
                    elif c < 0.6: bins[1]["count"] += 1
                    elif c < 0.7: bins[2]["count"] += 1
                    elif c < 0.8: bins[3]["count"] += 1
                    elif c < 0.9: bins[4]["count"] += 1
                    else: bins[5]["count"] += 1
                    idx = min(int(c / 0.05), 19)
                    fine[idx]["count"] += 1
                provider_list.append({"name": name, "bins": bins, "fine_bins": fine})
            self._json({"providers": provider_list})
            return
        if self.path.startswith("/api/modernbert"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            tasks = {}
            for r in records:
                if "modernbert_raw" not in (r.get("providers_used") or []):
                    continue
                heads = r.get("heads", {}).get("modernbert_raw", {})
                for tid, h in heads.items():
                    if not isinstance(h, dict):
                        continue
                    bucket = tasks.setdefault(tid, {"calls": 0, "top2_gaps": [], "norms": [], "labels": {}})
                    bucket["calls"] += 1
                    if h.get("top2_gap") is not None:
                        bucket["top2_gaps"].append(h["top2_gap"])
                    if h.get("embedding_norm") is not None:
                        bucket["norms"].append(h["embedding_norm"])
                    label = h.get("label")
                    if label:
                        bucket["labels"][label] = bucket["labels"].get(label, 0) + 1
            task_list = []
            for name, b in sorted(tasks.items()):
                top_labels = sorted(b["labels"].items(), key=lambda x: x[1], reverse=True)[:5]
                top2 = b["top2_gaps"]
                norms = b["norms"]
                top2_bins = [{"bin": f"{(i*0.1):.1f}-{((i+1)*0.1):.1f}", "count": 0} for i in range(10)]
                for g in top2:
                    idx = min(int(g / 0.1), 9)
                    top2_bins[idx]["count"] += 1
                norm_bins = None
                if norms:
                    lo = min(norms)
                    hi = max(norms)
                    if hi > lo:
                        step = (hi - lo) / 10 or 0.01
                        norm_bins = [{"bin": f"{lo + i*step:.2f}-{lo + (i+1)*step:.2f}", "count": 0} for i in range(10)]
                        for n in norms:
                            idx = min(int((n - lo) / step), 9)
                            norm_bins[idx]["count"] += 1
                task_list.append({
                    "name": name,
                    "calls": b["calls"],
                    "avg_top2": sum(top2)/len(top2) if top2 else None,
                    "avg_norm": sum(norms)/len(norms) if norms else None,
                    "top_labels": [{"label": l, "count": c} for l, c in top_labels],
                    "top2_bins": top2_bins,
                    "norm_bins": norm_bins,
                })
            self._json({"tasks": task_list})
            return
        if self.path.startswith("/api/agreement"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            pairs = {}
            for r in records:
                sha = r.get("input_sha256")
                if not sha:
                    continue
                heads = r.get("heads", {})
                provs = [p for p in r.get("providers_used", []) if p in heads]
                for i in range(len(provs)):
                    for j in range(i+1, len(provs)):
                        a, b = provs[i], provs[j]
                        ha, hb = heads.get(a, {}), heads.get(b, {})
                        labels_a = [v.get("label") for v in ha.values() if isinstance(v, dict)]
                        labels_b = [v.get("label") for v in hb.values() if isinstance(v, dict)]
                        key = f"{a} vs {b}"
                        bucket = pairs.setdefault(key, {"compared": 0, "agree": 0})
                        bucket["compared"] += 1
                        if set(labels_a) & set(labels_b):
                            bucket["agree"] += 1
            pair_list = [{"pair": k, "compared": v["compared"], "agree": v["agree"], "rate": v["agree"]/max(v["compared"],1)} for k,v in pairs.items()]
            self._json({"pairs": pair_list})
            return
        if self.path.startswith("/api/calibration"):
            feedback = self._filter_days(self._read_jsonl(Path("feedback") / "feedback.jsonl"))
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            index = {}
            for r in records:
                index.setdefault(r.get("input_sha256",""), []).append(r)
            bins = []
            for fb in feedback:
                sha = fb.get("input_sha256")
                task = fb.get("task")
                prov = fb.get("provider")
                true_label = fb.get("label")
                for r in index.get(sha, []):
                    heads = r.get("heads", {})
                    for p, h in heads.items():
                        if prov and p != prov:
                            continue
                        for v in h.values():
                            if isinstance(v, dict) and v.get("confidence") is not None:
                                conf = v["confidence"]
                                correct = 1 if v.get("label") == true_label else 0
                                bin_label = "<0.5" if conf < 0.5 else ("0.5-0.6" if conf < 0.6 else ("0.6-0.7" if conf < 0.7 else ("0.7-0.8" if conf < 0.8 else ("0.8-0.9" if conf < 0.9 else "0.9-1.0"))))
                                bins.append({"provider": p, "bin": bin_label, "correct": correct})
            # aggregate
            agg = {}
            for b in bins:
                key = (b["provider"], b["bin"])
                a = agg.setdefault(key, {"provider": b["provider"], "bin": b["bin"], "samples": 0, "correct": 0})
                a["samples"] += 1
                a["correct"] += b["correct"]
            out = []
            for a in agg.values():
                a["accuracy"] = a["correct"] / max(a["samples"], 1)
                out.append(a)
            self._json({"bins": out})
            return
        self.send_response(404)
        self.end_headers()


def cmd_dashboard(args: argparse.Namespace) -> int:
    """
    Start a local stdlib dashboard for provider telemetry.
    Binds to 127.0.0.1:8765 by default; open / no-open to control browser.
    """
    port = getattr(args, "port", 8765)
    host = getattr(args, "host", "127.0.0.1")
    days = getattr(args, "days", 7)
    no_open = getattr(args, "no_open", False)

    if not shutil.which("python3"):
        print("error: python3 not found", file=sys.stderr)
        return EXIT_ERROR

    import socketserver, threading
    from pathlib import Path

    base = LOG_DIR

    def make_handler(*a, **kw):
        return _DashboardHandler(*a, days=days, base=base, **kw)

    try:
        with socketserver.TCPServer((host, port), make_handler) as httpd:
            url = f"http://{host}:{port}/"
            print(f"dev-decisions dashboard — {url}  (Ctrl-C to stop)")
            if not no_open:
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:
                    pass
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped")
                return EXIT_OK
    except OSError as e:
        print(f"error: dashboard failed to start: {e}", file=sys.stderr)
        return EXIT_ERROR

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dev-decisions",
        description="Decision-model gates for git + ZCode workflows.",
    )
    p.add_argument("--version", action="version", version=f"dev-decisions {VERSION}")
    sub = p.add_subparsers(dest="command")

    # scan-staged
    sp = sub.add_parser("scan-staged", help="Scan staged diff for secrets and PII")
    sp.add_argument("--trigger", default="manual", help="Log this trigger label")
    sp.add_argument("--deep", action="store_true",
                    help="Run deep PII scan with local GLiNER span model (fully local, works on sensitive repos)")
    sp.set_defaults(func=cmd_scan_staged)

    # classify-diff
    sp = sub.add_parser("classify-diff", help="Classify staged diff with Decide/Jev")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider (local uses existing /private/tmp/gliner-decide venv)")
    sp.add_argument("--task", default=None,
                    help="Task to run: change, commit_audit, deps_risk, docs_drift, api_drift (default: auto-detect from diff)")
    sp.add_argument("--allow-vendor", action="store_true",
                    help="Allow vendor calls even if repo is sensitive")
    sp.add_argument("--trigger", default="manual", help="Log this trigger label")
    sp.set_defaults(func=cmd_classify_diff)

    # fleet-scan
    sp = sub.add_parser("fleet-scan", help="Scan fleet for API drift across repos")
    sp.add_argument("--root", default=None, help="Directory to scan (default: ~/Projects)")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default="local",
                    help="Provider to use (local recommended for fleet)")
    sp.add_argument("--task", default="api_drift", help="Task to run (default: api_drift)")
    sp.set_defaults(func=cmd_fleet_scan)

    # pr-gate
    sp = sub.add_parser("pr-gate", help="Classify PR diff and apply labels")
    sp.add_argument("branch", nargs="?", default=None, help="PR branch (default: current branch)")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider (local recommended)")
    sp.add_argument("--dry-run", action="store_true", help="Print labels without applying them")
    sp.add_argument("--fanout", action="store_true",
                    help="Speculative fan-out: per-file risky/action heads packed into ONE request alongside the standard heads")
    sp.add_argument("--diff-file", default=None,
                    help="Read the diff from a file instead of gh (offline; never applies labels)")
    sp.set_defaults(func=cmd_pr_gate)

    # plan-gate
    sp = sub.add_parser("plan-gate", help="Gate a plan against its acceptance criteria (coverage + scope matrix)")
    sp.add_argument("--draws", type=int, default=3,
                    help="Self-consistency draws per head (2026-10-02: single-draw flags proved jagged; default 3, majority/mean merged)")
    sp.add_argument("plan", help="Path to the plan markdown file")
    sp.add_argument("--criteria-file", default=None,
                    help="External requirements file (default: checkbox lines in the plan itself)")
    sp.add_argument("--tests", default=None,
                    help="Test root directory: collect pytest-style test files and score each criterion against the suite")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider")
    sp.set_defaults(func=cmd_plan_gate)

    # plan-surface
    sp = sub.add_parser("plan-surface",
                        help="Precompute the criterion/surface/dependency judgment graph for a plan (feed-forward decomposition input)")
    sp.add_argument("plan", help="Plan markdown file with checkbox acceptance criteria")
    sp.add_argument("--repo-root", default=None, help="Repository the plan targets (default: cwd)")
    sp.add_argument("--map-provider", default="auto",
                    help="Map layer provider chain (default: auto -> sys1 routing / plan_surface_map override)")
    sp.add_argument("--deps-provider", default="auto",
                    help="Dependency layer provider chain (default: auto -> plan_deps override)")
    sp.add_argument("--map-floor", type=float, default=None,
                    help="Module-confidence floor (default: sys1 plansurface MAP_FLOOR)")
    sp.add_argument("--map-top", type=int, default=None,
                    help="Max modules per criterion (default: sys1 plansurface MAP_TOP)")
    sp.add_argument("--dep-threshold", type=float, default=None,
                    help="Edge confidence cut (default: sys1 plansurface DEP_THRESHOLD)")
    sp.add_argument("--band", type=float, default=None,
                    help="Uncertainty band below the threshold (default: sys1 plansurface DEP_BAND)")
    sp.set_defaults(func=cmd_plan_surface)

    # docs-gate
    sp = sub.add_parser("docs-gate", help="Gate documentation coverage for a shipped plan (linked artifacts + staleness)")
    sp.add_argument("plan", help="Plan markdown with a '## Linked artifacts' section")
    sp.add_argument("--diff", default=None,
                    help="Git diff range or diff file: enables change-induced staleness claims (removed doc lines)")
    sp.add_argument("--repo-root", default=None, help="Repo root for resolving linked doc paths (default: cwd)")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider")
    sp.set_defaults(func=cmd_docs_gate)

    # arch-gate
    sp = sub.add_parser("arch-gate", help="Gate a plan against the documented architecture (conformance / drift / conventions-gap)")
    sp.add_argument("plan", help="Plan markdown file")
    sp.add_argument("--tech-doc", default=None, help="Path to the technical documentation (default: TECHNICAL-DOCUMENTATION.md in --repo-root)")
    sp.add_argument("--repo-root", default=None, help="Repo root (default: cwd)")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider")
    sp.set_defaults(func=cmd_arch_gate)

    # evidence-gate
    sp = sub.add_parser("evidence-gate", help="Gate QA evidence per acceptance criterion (fail-closed close-out check)")
    sp.add_argument("plan", help="Path to the plan markdown file (checkbox order defines C<i>)")
    sp.add_argument("evidence", help="Evidence file with '== C<i> ==' tagged blocks (commands, log tails, outputs)")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider")
    sp.set_defaults(func=cmd_evidence_gate)

    # plan-reconcile
    sp = sub.add_parser("plan-reconcile",
                        help="Grade a plan's surface artifact against the actual commit sequence (forward validation)")
    sp.add_argument("plan", help="Plan markdown file (must have a plan-surface artifact)")
    sp.add_argument("--repo-root", default=None, help="Repository the plan targets (default: artifact meta)")
    sp.add_argument("--artifact", default=None, help="Surface artifact path (default: surfaces dir by plan stem)")
    sp.add_argument("--since", default=None, help="Commit window start (default: plan created - 1 day)")
    sp.add_argument("--max-commits", type=int, default=400)
    sp.add_argument("--attribute-provider", default="jev",
                    help="Commit-to-criterion attribution layer ('' disables)")
    sp.set_defaults(func=cmd_plan_reconcile)

    # calibration
    sp = sub.add_parser("calibration",
                        help="Per-head graded-row counts, curves, and floor-fit readiness from both feedback stores")
    sp.add_argument("--min-rows", type=int, default=20, help="Graded rows required to fit (default 20)")
    sp.add_argument("--min-span", type=float, default=0.4, help="Confidence span required to fit (default 0.4)")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_calibration)

    # disposition
    sp = sub.add_parser("disposition", help="Record the human decision on a negative gate verdict (fixed / waived / overridden)")
    sp.add_argument("gate", help="Gate op: plan-gate | arch-gate | pr-gate | evidence-gate | docs-gate | zcode-gate")
    sp.add_argument("target", help="What the verdict was about (plan path, PR number, command)")
    sp.add_argument("--status", required=True, choices=["fixed", "waived", "overridden"],
                    help="fixed = gate was right and item reworked; waived = proceeding despite flag; overridden = gate was wrong")
    sp.add_argument("--reason", default=None, help="Required for waived/overridden — the calibration signal")
    sp.add_argument("--by", default=None, help="Who decided (default: $USER)")
    sp.set_defaults(func=cmd_disposition)

    # triage-issues
    sp = sub.add_parser("triage-issues", help="Batch-classify issues and apply labels")
    sp.add_argument("repo", nargs="?", default=None, help="Repo in owner/repo format (default: current)")
    sp.add_argument("--state", default="open", help="Issue state: open, closed, all")
    sp.add_argument("--limit", type=int, default=10, help="Max issues to classify")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider (local recommended)")
    sp.add_argument("--dry-run", action="store_true", help="Print labels without applying them (default)")
    sp.set_defaults(func=cmd_triage_issues)

    # changelog
    sp = sub.add_parser("changelog", help="Generate Keep-a-Changelog markdown since a ref")
    sp.add_argument("--since", required=True, help="Git ref (tag/commit) to start from")
    sp.add_argument("--next-version", default="Unreleased", help="Version header (default: Unreleased)")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider (local recommended)")
    sp.add_argument("--write", action="store_true", help="Write/append to CHANGELOG.md")
    sp.set_defaults(func=cmd_changelog)

    # zcode-gate
    sp = sub.add_parser("zcode-gate", help="ZCode PreToolUse hook (reads JSON from stdin)")
    sp.set_defaults(func=cmd_zcode_gate)

    # install-hooks / remove-hooks
    sp = sub.add_parser("install-hooks", help="Install git hooks into a repo")
    sp.add_argument("repo", nargs="?", default=None, help="Repo path (default: current)")
    sp.add_argument("--force", action="store_true", help="Overwrite existing hooks")
    sp.set_defaults(func=cmd_install_hooks)

    sp = sub.add_parser("remove-hooks", help="Remove dev-decisions git hooks")
    sp.add_argument("repo", nargs="?", default=None, help="Repo path (default: current)")
    sp.set_defaults(func=cmd_remove_hooks)

    # bulk-install
    sp = sub.add_parser("bulk-install", help="Install hooks in every git repo under a directory")
    sp.add_argument("--root", default=None, help="Directory to scan (default: ~/Projects)")
    sp.add_argument("--force", action="store_true", help="Overwrite existing hooks")
    sp.set_defaults(func=cmd_bulk_install)

    # status
    sp = sub.add_parser("status", help="Fleet view: hooks, sensitivity, env files across repos")
    sp.add_argument("--root", default=None, help="Directory to scan (default: ~/Projects)")
    sp.set_defaults(func=cmd_status)

    # log
    sp = sub.add_parser("log", help="Show decision log")
    sp.add_argument("--since", help="Only show entries after this ISO timestamp")
    sp.add_argument("--tail", type=int, help="Last N entries")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_log)

    # feedback — calibration ground truth
    sp = sub.add_parser("feedback", help="Record human feedback for a previous classification")
    sp.add_argument("input_sha256", help="Input hash from a previous classify-diff/log entry")
    sp.add_argument("--task", required=True, help="Task name (e.g. change, deps_risk)")
    sp.add_argument("--label", required=True, help="Correct label (ground truth)")
    sp.add_argument("--provider", default=None, help="Provider tag (e.g. decide, local, modernbert_raw)")
    sp.add_argument("--note", default=None, help="Optional free-text note")
    sp.add_argument("--head-id", default=None, help="Head the feedback grades (additive; per-head calibration)")
    sp.set_defaults(func=cmd_feedback)

    # dashboard — live local monitoring
    sp = sub.add_parser("dashboard", help="Start local dashboard for provider telemetry")
    sp.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765)")
    sp.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    sp.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    sp.add_argument("--no-open", action="store_true", help="Do not open browser automatically")
    sp.set_defaults(func=cmd_dashboard)

    # config
    sp = sub.add_parser("config", help="Show effective config")
    sp.set_defaults(func=cmd_config)

    # doctor
    sp = sub.add_parser("doctor", help="Environment check")
    sp.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
