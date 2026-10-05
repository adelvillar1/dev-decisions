"""argument parser + main dispatch."""

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

from .workflow import cmd_bulk_install, cmd_classify_diff, cmd_config, cmd_doctor, cmd_fleet_scan, cmd_install_hooks, cmd_log, cmd_remove_hooks, cmd_scan_staged, cmd_status, cmd_zcode_gate
from .gates import cmd_arch_gate, cmd_calibration, cmd_disposition, cmd_docs_gate, cmd_evidence_gate, cmd_feedback, cmd_judge, cmd_plan_gate, cmd_plan_reconcile, cmd_plan_surface, cmd_pr_gate
from .corpora import cmd_changelog, cmd_triage_issues, cmd_uc_gate, cmd_ux_gate, cmd_ux_surface
from .dashboard import cmd_dashboard
from .config import DEFAULT_MAX_DIFF_CHARS, EXIT_OK, VERSION
from .judgment import PROVIDER_CHOICES

# section: cli (moved verbatim from dev_decisions.py)

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

    # judge (generic)
    sp = sub.add_parser("judge",
                        help="Ad-hoc heads over one text, classified through sys1 and logged as a store row")
    sp.add_argument("heads", help="JSON: [{id, kind: choice|noul, task, labels}] (one head object also accepted)")
    sp.add_argument("--text", default=None, help="The text to judge")
    sp.add_argument("--text-file", default=None, help="File whose contents are judged (instead of --text)")
    sp.add_argument("--task-id", default="judge", help="Task id recorded with the row (default: judge)")
    sp.add_argument("--description", default=None, help="Task description")
    sp.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                    help="Override config provider")
    sp.add_argument("--max-chars", type=int, default=DEFAULT_MAX_DIFF_CHARS,
                    help="Cap on judged text length")
    sp.set_defaults(func=cmd_judge)

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

    # ux-surface / ux-gate
    sp = sub.add_parser("ux-surface",
                        help="Build the UX surface artifact: routes x designed states x capture verification")
    sp.add_argument("app", help="App name (artifact key)")
    sp.add_argument("--ux-docs", default="docs/ux", help="Route contracts dir (default docs/ux)")
    sp.add_argument("--flags-dir",
                    default=str(Path.home() / ".zcode/workspace/default/ux-capture-probe/shots"),
                    help="ux-capture kit flag reports dir")
    sp.add_argument("--entry", default=None,
                    help="Entry route name (exempt from orphan check; default: app name)")
    sp.set_defaults(func=cmd_ux_surface)

    sp = sub.add_parser("ux-gate",
                        help="Gate an app's UX surface: confirmed drift fails, ungraded flags warn")
    sp.add_argument("app", help="App name (surface artifact key)")
    sp.add_argument("--surface", default=None, help="Surface artifact path override")
    sp.set_defaults(func=cmd_ux_gate)

    # uc-gate
    sp = sub.add_parser("uc-gate",
                        help="Issues vs existing functionality vs planned design: the per-issue 2x2 coverage matrix")
    sp.add_argument("issues", help="Issues contract markdown (## Issues with '- [ ] <id>: statement')")
    sp.add_argument("--fs", required=True, help="Existing-functionality inventory (e.g. FUNCTIONAL-SPECIFICATIONS.md)")
    sp.add_argument("--design", required=True, help="Planned-design inventory (e.g. the plan's Approach)")
    sp.add_argument("--app", default=None, help="App/plan name for the log row")
    sp.add_argument("--provider", default=None, help="Override config provider")
    sp.add_argument("--cite-floor", type=float, default=0.15,
                    help="Citation probability floor (default 0.15; semantic-find lesson)")
    sp.set_defaults(func=cmd_uc_gate)

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
