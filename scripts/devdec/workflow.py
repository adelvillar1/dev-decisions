"""scan-staged, classify-diff, zcode-gate, hooks, fleet ops."""

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

from .config import CONFIG_FILE, EXIT_BLOCK, EXIT_ERROR, EXIT_OK, EXIT_WARN, _log_dir, load_config, log_record
from .judgment import _call_local_pii_scan, _call_local_provider, _call_modernbert_provider, _diff_paths, _is_doc_path, _legacy_classify, _sys1_classify, _sys1_classify_single, detect_task_from_diff, get_task_heads, sys1
from .gitops import effective_diff, get_repo_root, hook_exit, last_commit_diff, push_range_diff, read_push_refs, repo_name, scan_text, staged_diff

# section: workflow (moved verbatim from dev_decisions.py)

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

    # tabular lane routing: [classify] by_task may send a task to sdm1
    by_task = cfg["classify"].get("by_task") or {}
    effective_provider = by_task.get(task) or provider

    # ── classify: sys1 library if importable, inline providers otherwise ──
    if effective_provider == "sdm1":
        from .tabular import classify_text_via_sdm1

        outcome = classify_text_via_sdm1(task, diff, cfg)
    elif sys1 is not None:
        outcome = _sys1_classify(effective_provider, task, diff, cfg)
    else:
        outcome = _legacy_classify(effective_provider, task, diff, cfg)

    results: dict[str, dict] = outcome["results"]
    providers_used: list[str] = outcome["providers_used"]
    provider_telemetry: dict[str, dict] = outcome["telemetry"]
    escalated: bool = outcome["escalated"]
    gate_summary: list[str] = outcome["summary"]
    elapsed_ms: int = outcome["latency_ms"]

    if not results:
        # Log the failed/declined run too — a by_task sdm1 decline is a real
        # calibration row, not a silence (C1 of the tabular-decision-lane plan).
        log_record({
            "op": "classify-diff",
            "repo": repo_name(repo),
            "trigger": args.trigger or "manual",
            "task": task,
            "provider": effective_provider,
            "providers_used": providers_used,
            "input_chars": len(diff),
            "input_sha256": hashlib.sha256(diff.encode()).hexdigest()[:16],
            "heads": {},
            "overridden": [],
            "verdict": "escalated",
            "escalated": True,
            "latency_ms": elapsed_ms,
            "telemetry": provider_telemetry,
        })
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

    # risk-prior composition (C6): cached table read only — never a network
    # call in the hook path (the batch-only rule).
    risk_prior_info = None
    if getattr(args, "with_risk_prior", False):
        from .tabular import load_risk_prior, risk_prior_for_paths

        risk_prior_info = risk_prior_for_paths(load_risk_prior(), changed_paths)
        if risk_prior_info.get("prior") is not None:
            print(
                f"  [risk-prior] {risk_prior_info['dir']}: revert prior "
                f"{risk_prior_info['prior']:.2f} (cached table, no network)"
            )

    print("\n".join(gate_summary))
    if escalated:
        print(f"\n⚠ Low confidence or null verdict — human review recommended (floor {cfg['classify']['confidence_floor']}).")

    # Log
    log_record({
        "op": "classify-diff",
        "repo": repo_name(repo),
        "trigger": args.trigger or "manual",
        "task": task,
        "provider": effective_provider,
        "providers_used": providers_used,
        "input_chars": len(diff),
        "input_sha256": hashlib.sha256(diff.encode()).hexdigest()[:16],
        "heads": _sanitize_for_log(results),
        "overridden": overridden,
        "risk_prior": risk_prior_info,
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

    # sdm1 library (tabular decision lane — batch-only, hosted TabPFN)
    from .judgment import sdm1
    from .tabular import sdm1_ready

    sdm1_ok, sdm1_reason = sdm1_ready()
    if sdm1 is not None:
        try:
            sdm1_version = sdm1.__version__
        except Exception:
            sdm1_version = "?"
        print(f"sdm1:   v{sdm1_version} ({sdm1_reason})")
        try:
            health = sdm1.health_report()
            for pid, info in sorted(health.items()):
                tick = "✓" if info.get("available") else "✗"
                print(f"  {tick} {pid}: {info.get('reason', '')}")
        except Exception as e:
            print(f"  sdm1 health check failed: {e}")
    else:
        print(f"sdm1:   not importable — tabular lane unavailable ({sdm1_reason})")

    # sem1 library (semantic embeddings lane — batch-only, eval-only)
    from .judgment import sem1 as _sem1
    from .semantics import sem1_ready as _sem1_ready

    _sem1_ok, _sem1_reason = _sem1_ready()
    if _sem1 is not None:
        try:
            print(f"sem1:   v{_sem1.__version__} ({_sem1_reason})")
        except Exception:
            print(f"sem1:   available ({_sem1_reason})")
    else:
        print(f"sem1:   not importable — semantic lane unavailable ({_sem1_reason})")

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


