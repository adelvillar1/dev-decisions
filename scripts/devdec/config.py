"""constants, config load/merge, JSONL telemetry."""

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

# Defined before the judgment import: judgment's gitops needs this literal, and
# importing it from config at module scope would otherwise close a cycle.
DEFAULT_MAX_DIFF_CHARS = 12_000

from .judgment import sys1

# section: config (moved verbatim from dev_decisions.py)

# ── constants ────────────────────────────────────────────────────────────────

VERSION = "0.3.0"
CONFIG_DIR = Path.home() / ".config" / "dev-decisions"
CONFIG_FILE = CONFIG_DIR / "config.toml"
LOG_DIR = Path.home() / ".local" / "share" / "dev-decisions" / "logs"
SURFACES_DIR = Path.home() / ".local" / "share" / "dev-decisions" / "surfaces"
SKILL_DIR = Path.home() / ".agents" / "skills" / "dev-decisions"
HOOKS_DIR = SKILL_DIR / "hooks"

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


