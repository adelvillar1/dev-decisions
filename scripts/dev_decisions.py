#!/usr/bin/env python3
"""
dev-decisions — decision-model gates for git + ZCode workflows.

Subcommands: scan-staged, classify-diff, zcode-gate, install-hooks,
             remove-hooks, log, config, doctor

Stdlib-only (urllib, tomllib, argparse, hashlib, json, os, re, sqlite3, stat,
subprocess, sys, time, datetime). Runs on any python3 ≥ 3.10.

Optional local GLiNER (Phase 2): uv-managed Python 3.12 venv with gliner2[local].
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ── constants ────────────────────────────────────────────────────────────────

VERSION = "0.2.0"
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
) -> dict:
    """
    Call an OpenAI-compatible chat-completions endpoint.
    Returns the parsed JSON response body, or raises on final failure.
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
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                api_url, data=data, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
            parsed = json.loads(raw)
            # surface provider errors
            if "error" in parsed:
                msg = parsed["error"].get("message", str(parsed["error"]))
                if "warming" in msg.lower() and attempt < max_retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise RuntimeError(f"Provider error: {msg}")
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
                raise RuntimeError(last_err) from e
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
            else:
                raise RuntimeError(f"Request failed: {last_err}") from e

    raise RuntimeError(last_err)  # unreachable, satisfies type checker


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
    "change": lambda: {"decide": _build_decide_heads(), "jev": _build_jev_heads(), "local": _build_local_heads()},
    "commit_audit": _build_commit_audit_heads,
    "deps_risk": _build_deps_risk_heads,
    "docs_drift": _build_docs_drift_heads,
    "api_drift": _build_api_drift_heads,
    "pr_gate": _build_pr_gate_heads,
    "issue_triage": _build_issue_triage_heads,
    "safety": _build_safety_heads,
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


def _call_local_provider(diff: str, heads: list[dict], cfg: dict) -> dict:
    """
    Call local GLiNER2 via the existing /private/tmp/gliner-decide venv.
    Returns {task_text: {label, confidence}} or raises on failure.
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

    proc = subprocess.run(
        [str(venv_python), "-c", inline, diff[: cfg["scan"]["max_diff_chars"]], tasks_json],
        capture_output=True, text=True, timeout=cfg["providers"]["request_timeout_seconds"],
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Local GLiNER failed: {proc.stderr[:300]}")

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        parsed = {"_raw": proc.stdout[:200]}

    # Normalize: keys are task[:40] from caller
    out: dict = {}
    for tid, entry in parsed.items():
        if isinstance(entry, dict):
            out[tid] = {
                "label": entry.get("label"),
                "confidence": entry.get("confidence"),
            }
        else:
            out[tid] = {"_raw": str(entry)}
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
    heads = get_task_heads(task, provider.split("+")[0])  # base provider for heads
    results: dict[str, dict] = {}
    providers_used: list[str] = []
    t0 = time.monotonic()

    # Decide call
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
                resp = _call_openai_compatible(
                    pcfg["decide_api_url"],
                    decide_key,
                    pcfg["decide_model"],
                    messages,
                    schema={"classifications": heads},
                    timeout=pcfg["request_timeout_seconds"],
                    max_retries=pcfg["max_retries"],
                    backoff=pcfg["retry_backoff_seconds"],
                )
                parsed = _parse_decide_response(resp)
                results["decide"] = parsed
                providers_used.append("decide")
            except Exception as e:
                print(f"⚠ Decide call failed: {e}", file=sys.stderr)

    # Jev call — SDK wire shape: POST /v1/systemone with { state, questions, model }
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
                }).encode()
                req = urllib.request.Request(
                    pcfg["jev_api_url"],
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {jev_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=pcfg["request_timeout_seconds"]) as resp:
                    raw = json.loads(resp.read().decode())

                parsed = _parse_jev_response(raw, [q["id"] for q in questions])
                results["jev"] = parsed
                providers_used.append("jev")
            except Exception as e:
                print(f"⚠ Jev call failed: {e}", file=sys.stderr)

    # Local GLiNER call — runs in existing /private/tmp/gliner-decide venv
    if provider in ("local", "both"):
        try:
            local_heads = get_task_heads(task, "local")
            parsed = _call_local_provider(diff, local_heads, cfg)
            results["local"] = parsed
            providers_used.append("local")
        except Exception as e:
            print(f"⚠ Local GLiNER call failed: {e}", file=sys.stderr)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if not results:
        print("No provider succeeded — diff unclassified.")
        return EXIT_WARN if cfg["classify"]["block_on_classification"] else EXIT_OK

    # ── interpret ────────────────────────────────────────────────────────
    escalated = False
    gate_summary: list[str] = []

    for prov, heads_result in results.items():
        gate_summary.append(f"\n── {prov} ──")
        for key, entry in heads_result.items():
            if key.startswith("_"):
                continue  # skip meta keys like _raw
            if not isinstance(entry, dict):
                # null/declined answer from provider
                gate_summary.append(f"  {key}: (declined)")
                escalated = True
                continue
            label = entry.get("label") or entry.get("score") or entry.get("noul") or entry.get("_raw", "?")
            conf = entry.get("confidence")
            label_str = str(label)[:60]
            if conf is not None:
                gate_summary.append(f"  {key}: {label_str} (conf {conf:.2f})")
            else:
                gate_summary.append(f"  {key}: {label_str}")

            # escalation checks
            if conf is not None and conf < cfg["classify"]["confidence_floor"]:
                escalated = True
            if entry.get("label") is None and entry.get("score") is None and entry.get("noul") is None:
                escalated = True

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

        # Run safety task via local provider (fast, fully local) to check reversibility
        try:
            heads = get_task_heads("safety", "local")
            safety_result = _call_local_provider(git_cmd, heads, cfg)
            for key, val in safety_result.items():
                if isinstance(val, dict):
                    label = val.get("label")
                    if label:
                        if "reversible" in key.lower():
                            reversible = label if isinstance(label, str) else str(label)
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
            heads = get_task_heads(task, provider if provider != "both" else "decide")

            # Run the classification inline (simplified)
            if provider == "local":
                try:
                    result = _call_local_provider(diff, heads, cfg)
                except Exception:
                    continue
            elif provider == "decide":
                continue  # skip vendor in fleet-scan unless explicitly requested
            else:
                continue

            # Extract api_drift answers
            public_api = result.get("Does this diff modify any public API surface (routes, schemas, exported functions, types, interfaces, RPC methods)?", {})
            breaking = result.get("Does this diff introduce a breaking change to the public API?", {})

            pub_label = public_api.get("label", "?")
            brk_label = breaking.get("label", "?")
            sev_label = result.get("If there is a breaking change, how severe is it? high = requires immediate migration; medium = requires migration but has deprecation path; low = backward-compatible.", {}).get("label", "?")

            # confidence from the highest-confidence head
            confs = [v.get("confidence") for v in result.values() if isinstance(v, dict) and v.get("confidence") is not None]
            conf_str = f"{max(confs):.2f}" if confs else "—"

            flag = ""
            if pub_label == "yes" or brk_label == "yes":
                flag = " ⚠"
                escalated_count += 1

            print(f"  {repo.name:<32} {pub_label:<12} {brk_label:<10} {sev_label:<10} {conf_str:<8}{flag}")
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
    heads = get_task_heads(task, provider.split("+")[0])

    try:
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
            )
            parsed = _parse_decide_response(result)
        elif provider == "jev":
            questions = get_task_heads(task, "jev")
            payload_questions = {q["id"]: {"type": q["type"], "instructions": q["instructions"], "criteria": q.get("criteria")} for q in questions}
            body = json.dumps({"state": {"diff": diff[:DEFAULT_MAX_DIFF_CHARS], "task": task}, "questions": payload_questions, "model": cfg["providers"]["jev_model"]})
            resp = _call_jev_raw(body, _env_key("jev"))
            parsed = _parse_jev_response(resp, [q["id"] for q in questions])
        else:
            parsed = _call_local_provider(diff, heads, cfg)
    except Exception as e:
        print(f"error: classification failed: {e}", file=sys.stderr)
        return EXIT_ERROR

    # Extract labels (multi-label support)
    labels_to_apply: list[str] = []
    for key, val in parsed.items():
        if isinstance(val, dict):
            label = val.get("label")
            if label:
                if isinstance(label, list):
                    labels_to_apply.extend(label)
                else:
                    labels_to_apply.append(label)

    # Deduplicate
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
        "task": task,
        "provider": provider,
        "labels": labels_to_apply,
        "dry_run": dry_run,
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

    for issue in issues:
        num = issue["number"]
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        text = f"{title}\n\n{body}"[:DEFAULT_MAX_DIFF_CHARS]

        try:
            if provider in ("decide", "both"):
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
                )
                parsed = _parse_decide_response(result)
            elif provider == "jev":
                questions = get_task_heads(task, "jev")
                payload_questions = {q["id"]: {"type": q["type"], "instructions": q["instructions"], "criteria": q.get("criteria")} for q in questions}
                body = json.dumps({"state": {"text": text[:DEFAULT_MAX_DIFF_CHARS], "task": task}, "questions": payload_questions, "model": cfg["providers"]["jev_model"]})
                resp = _call_jev_raw(body, _env_key("jev"))
                parsed = _parse_jev_response(resp, [q["id"] for q in questions])
            else:
                parsed = _call_local_provider(text, heads, cfg)
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
                    # infer kind/priority from key
                    if "kind" in key.lower():
                        kind = label if isinstance(label, str) else str(label)
                    elif "priority" in key.lower():
                        priority = label if isinstance(label, str) else str(label)

        labels_to_apply = sorted(set(labels_to_apply))
        print(f"  {num:<6} {kind:<12} {priority:<10} {', '.join(labels_to_apply):<30} {title[:40]}")

        if not dry_run and labels_to_apply:
            for label in labels_to_apply:
                try:
                    _call_gh(["issue", "edit", str(num), "--add-label", label])
                except RuntimeError as e:
                    print(f"    ✗ failed to apply {label}: {e}", file=sys.stderr)

    log_record({
        "op": "triage-issues",
        "count": len(issues),
        "task": task,
        "provider": provider,
        "dry_run": dry_run,
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
    heads = get_task_heads("change", provider.split("+")[0])

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

        try:
            if provider in ("decide", "both"):
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
                )
                parsed = _parse_decide_response(result)
            elif provider == "jev":
                questions = get_task_heads("change", "jev")
                payload_questions = {q["id"]: {"type": q["type"], "instructions": q["instructions"], "criteria": q.get("criteria")} for q in questions}
                body = json.dumps({"state": {"subject": subject, "diff": diff[:DEFAULT_MAX_DIFF_CHARS]}, "questions": payload_questions, "model": cfg["providers"]["jev_model"]})
                resp = _call_jev_raw(body, _env_key("jev"))
                parsed = _parse_jev_response(resp, [q["id"] for q in questions])
            else:
                parsed = _call_local_provider(diff, heads, cfg)
        except Exception as e:
            sections["Changed"].append(subject)
            continue

        # Map diff_type to section
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


# ── argparse ─────────────────────────────────────────────────────────────────

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
    sp.add_argument("--provider", choices=["decide", "jev", "local", "both"], default=None,
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
    sp.add_argument("--provider", choices=["decide", "jev", "local", "both"], default="local",
                    help="Provider to use (local recommended for fleet)")
    sp.add_argument("--task", default="api_drift", help="Task to run (default: api_drift)")
    sp.set_defaults(func=cmd_fleet_scan)

    # pr-gate
    sp = sub.add_parser("pr-gate", help="Classify PR diff and apply labels")
    sp.add_argument("branch", nargs="?", default=None, help="PR branch (default: current branch)")
    sp.add_argument("--provider", choices=["decide", "jev", "local", "both"], default=None,
                    help="Override config provider (local recommended)")
    sp.add_argument("--dry-run", action="store_true", help="Print labels without applying them")
    sp.set_defaults(func=cmd_pr_gate)

    # triage-issues
    sp = sub.add_parser("triage-issues", help="Batch-classify issues and apply labels")
    sp.add_argument("repo", nargs="?", default=None, help="Repo in owner/repo format (default: current)")
    sp.add_argument("--state", default="open", help="Issue state: open, closed, all")
    sp.add_argument("--limit", type=int, default=10, help="Max issues to classify")
    sp.add_argument("--provider", choices=["decide", "jev", "local", "both"], default=None,
                    help="Override config provider (local recommended)")
    sp.add_argument("--dry-run", action="store_true", help="Print labels without applying them (default)")
    sp.set_defaults(func=cmd_triage_issues)

    # changelog
    sp = sub.add_parser("changelog", help="Generate Keep-a-Changelog markdown since a ref")
    sp.add_argument("--since", required=True, help="Git ref (tag/commit) to start from")
    sp.add_argument("--next-version", default="Unreleased", help="Version header (default: Unreleased)")
    sp.add_argument("--provider", choices=["decide", "jev", "local", "both"], default=None,
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
