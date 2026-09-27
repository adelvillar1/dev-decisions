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

# --provider choices: dynamic from the sys1 registry, so candidate models
# (clm, kev, tev1, decide_1b, ...) show up as soon as sys1 registers them —
# plus the fan-out aliases. Falls back to the four core ids when sys1 isn't
# installed (self-contained mode).
_FALLBACK_PROVIDER_IDS = ["decide", "jev", "local", "modernbert"]
_PROVIDER_ALIASES = ["both", "all", "core", "optin", "candidates"]


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
    chain = _sys1_chain(provider)
    result = sys1.classify(chain, task, text, cfg=cfg, log=False)
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        answers = result.answers.get(mapped)
        if answers:
            return {"provider": mapped, "answers": answers, "telemetry": result.telemetry.get(mapped, {})}
    return {"provider": chain[0] if chain else "", "answers": {}, "telemetry": {}}


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
        "provider": "decide",        # decide | jev | both
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


def detect_task_from_diff(diff: str) -> str:
    """Heuristic task detection from diff content."""
    diff_lower = diff.lower()
    # dependency files
    dep_patterns = ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                    "requirements.txt", "pyproject.toml", "cargo.toml", "go.mod", "gemfile", "pom.xml"]
    if any(p in diff_lower for p in dep_patterns):
        return "deps_risk"
    # docs-only diff
    doc_patterns = [".md", ".rst", ".txt", "docs/", "documentation/"]
    if all(p in diff_lower for p in [".md"]) or "docs/" in diff_lower:
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
    tasks_json = json.dumps({h["task"][:40]: h for h in heads})
    inline = f'''
import json, sys, io, contextlib
from gliner2 import GLiNER2

# Suppress model init banner
old_stdout = sys.stdout
sys.stdout = io.StringIO()

model = GLiNER2.from_pretrained({cfg["providers"]["local_model"]!r})
text = sys.argv[1]
tasks = json.loads(sys.argv[2])
results = {{}}
for tid, task in tasks.items():
    r = model.classify_text(text, tasks={{"label": task["labels"]}}, include_confidence=True)
    entry = {{}}
    if isinstance(r, dict):
        inner = r.get("label") or r
        if isinstance(inner, dict):
            entry["label"] = inner.get("label")
            entry["confidence"] = inner.get("confidence")
        else:
            entry["label"] = str(inner)
    results[tid] = entry

# Restore stdout and print only the JSON result
sys.stdout = old_stdout
print(json.dumps(results))
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

    return EXIT_WARN if (secrets and not block) else EXIT_OK


def cmd_classify_diff(args: argparse.Namespace) -> int:
    repo = get_repo_root()
    if not repo:
        print("error: not in a git repository", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(repo)

    # vendor guard: run local scan first
    diff = effective_diff(repo, cfg["scan"]["max_diff_chars"])
    if not diff:
        print("No staged changes to classify.")
        return EXIT_OK

    secrets, pii = scan_text(diff, cfg)
    if secrets:
        print("⚠ Secrets detected — skipping vendor classification (policy).")
        return EXIT_WARN

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
    task = args.task or detect_task_from_diff(diff)

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
        return EXIT_WARN if cfg["classify"]["block_on_classification"] else EXIT_OK

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
        "verdict": "escalated" if escalated else "pass",
        "escalated": escalated,
        "latency_ms": elapsed_ms,
        "telemetry": provider_telemetry,
    })

    if escalated and cfg["classify"]["block_on_classification"]:
        return EXIT_BLOCK
    return EXIT_OK if not escalated else EXIT_WARN


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
    destructive_patterns = [
        r"git\s+push\s+.*--force",
        r"rm\s+-rf\s+",
        r"DROP\s+TABLE",
        r"TRUNCATE\s+TABLE",
        r"--no-verify",
        r"alembic\s+.*(?:upgrade|downgrade).*head",
        r"migrate\s+.*(?:up|down).*production",
        r"rails\s+db:migrate",
        r"kubectl\s+delete",
    ]
    destructive_hits = [p for p in destructive_patterns if re.search(p, git_cmd, re.IGNORECASE)]

    if destructive_hits:
        print(f"[gate] destructive pattern detected in: {git_cmd}", file=sys.stderr)
        print(f"[gate] patterns: {', '.join(destructive_hits)}", file=sys.stderr)

        # Regex match is authoritative for destructive flag; model refines reversibility
        destructive = True
        reversible = "unknown"
        reversible_conf = None

        # Run safety task via local provider (fast, fully local) to check reversibility
        try:
            if sys1 is not None:
                safety_telemetry: dict = {}
                safety_result = {}
                single = _sys1_classify_single("local", "safety", git_cmd, cfg)
                safety_result = single.get("answers", {})
                safety_telemetry = single.get("telemetry", {})
            else:
                heads = get_task_heads("safety", "local")
                safety_telemetry = {}
                safety_result = _call_local_provider(git_cmd, heads, cfg, telemetry=safety_telemetry)
            for key, val in safety_result.items():
                if isinstance(val, dict):
                    label = val.get("label") or val.get("noul") or val.get("score")
                    conf = val.get("confidence")
                    if label:
                        if "reversible" in key.lower():
                            reversible = label if isinstance(label, str) else str(label)
                            reversible_conf = conf
        except Exception as e:
            print(f"[gate] safety task failed: {e} — reversibility unknown", file=sys.stderr)

        print(f"[gate] safety task: DESTRUCTIVE (reversible={reversible})", file=sys.stderr)
        reason = f"Destructive operation detected (reversible={reversible})."
        if advisory_only:
            print(f"[gate] advisory_only=true — proceeding with warning: {reason}", file=sys.stderr)
            log_record({
                "op": "zcode-gate",
                "tool": tool,
                "command": git_cmd[:200],
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
                "verdict": "block",
                "destructive": True,
                "reversible": reversible,
                "reversible_confidence": reversible_conf,
                "advisory_only": False,
            })
            return EXIT_BLOCK

    # ── git commit / push gating ──────────────────────────────────────────
    if not re.match(r"git\s+(commit|push)\b", git_cmd):
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

    ok &= write_hook("pre-commit", f'exec "$HOME/.local/bin/dev-decisions" scan-staged --trigger git-pre-commit')
    ok &= write_hook("pre-push", f'exec "$HOME/.local/bin/dev-decisions" classify-diff --trigger git-pre-push')

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


def _discover_repos(root: Path) -> list[Path]:
    """Find git repos one level deep under root (non-recursive past that)."""
    repos: list[Path] = []
    if not root.is_dir():
        return repos
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / ".git").is_dir():
            repos.append(child)
        else:
            # one extra level for nested project layouts
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and (grandchild / ".git").is_dir():
                    repos.append(grandchild)
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


def _find_result_by_prefix(result: dict, prefix: str) -> dict:
    """Find the first result key that starts with `prefix` (handles task[:40] truncation)."""
    for key in result:
        if key.startswith(prefix):
            return result[key]
    return {}


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
        print(f"PR #{pr_num} has no diff.")
        return EXIT_OK

    # Classify with pr_gate task
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]
    task = "pr_gate"

    try:
        if sys1 is not None:
            single = _sys1_classify_single(provider, task, diff, cfg)
            parsed: dict = single.get("answers", {})
            item_telemetry: dict = single.get("telemetry", {})
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

    # Extract labels (multi-label support)
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

    print(f"PR #{pr_num} classification:")
    print(f"  labels: {', '.join(labels_to_apply) or 'none'}")

    if dry_run:
        print("  (dry-run — labels not applied)")
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
        "repo": repo_name(get_repo_root() or Path(".")),
        "task": task,
        "provider": provider,
        "labels": labels_to_apply,
        "dry_run": dry_run,
        "verdict": "pass" if not labels_to_apply else "applied",
        "telemetry": item_telemetry,
    })
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

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "input_sha256": input_sha256,
        "task": task,
        "provider": provider,
        "label": label,
        "note": note,
    }

    try:
        feedback_dir = LOG_DIR / "feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        feedback_path = feedback_dir / "feedback.jsonl"
        with open(feedback_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        print(f"Logged feedback for {input_sha256} (task={task}, label={label})")
        return EXIT_OK
    except Exception as e:
        print(f"error: failed to write feedback: {e}", file=sys.stderr)
        return EXIT_ERROR


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

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_DASHBOARD_HTML.encode())))
            self.end_headers()
            self.wfile.write(_DASHBOARD_HTML.encode())
            return
        if self.path.startswith("/api/summary"):
            records = self._filter_days(self._read_jsonl(Path("2026") / "09" / "26" / "events.jsonl"))
            # fallback: read all dated dirs
            if not records and (self._base / "2026").exists():
                all_records = []
                for p in sorted((self._base / "2026").rglob("events.jsonl")):
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
            records = self._filter_days(self._read_jsonl(Path("2026") / "09" / "26" / "events.jsonl"))
            if not records and (self._base / "2026").exists():
                all_records = []
                for p in sorted((self._base / "2026").rglob("events.jsonl")):
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
            records = self._filter_days(self._read_jsonl(Path("2026") / "09" / "26" / "events.jsonl"))
            if not records and (self._base / "2026").exists():
                all_records = []
                for p in sorted((self._base / "2026").rglob("events.jsonl")):
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
            records = self._filter_days(self._read_jsonl(Path("2026") / "09" / "26" / "events.jsonl"))
            if not records and (self._base / "2026").exists():
                all_records = []
                for p in sorted((self._base / "2026").rglob("events.jsonl")):
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
            records = self._filter_days(self._read_jsonl(Path("2026") / "09" / "26" / "events.jsonl"))
            if not records and (self._base / "2026").exists():
                all_records = []
                for p in sorted((self._base / "2026").rglob("events.jsonl")):
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
    sp.set_defaults(func=cmd_pr_gate)

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
