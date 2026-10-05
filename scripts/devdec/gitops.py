"""git helpers, secret/PII scanner, hook exits."""

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

from .config import DEFAULT_MAX_DIFF_CHARS

# section: gitops (moved verbatim from dev_decisions.py)

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
    from .config import EXIT_OK, EXIT_WARN

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


