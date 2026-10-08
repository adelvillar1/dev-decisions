"""pr/plan/evidence/docs/arch gates, disposition, reconcile, calibration, feedback."""

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

from .config import DEFAULT_MAX_DIFF_CHARS, EXIT_ERROR, EXIT_OK, EXIT_WARN, LOG_DIR, SURFACES_DIR, load_config, log_record
from .judgment import _SYS1_PROVIDER_REMAP, _call_jev_raw, _call_local_provider, _call_modernbert_provider, _call_openai_compatible, _env_key, _extract_labels, _parse_decide_response, _parse_jev_response, _plansurface, _sys1_chain, _sys1_classify_fanout, _sys1_classify_single, _sys1_consistency, get_task_heads, sys1
from .gitops import get_repo_root, repo_name
from .workflow import _sha16

# section: gates (moved verbatim from dev_decisions.py)

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


_UX_CONTRACT_DIR = "docs/ux"


def _plan_structure(text: str) -> dict:
    """Mechanical structural checks over the plan document (no model).
    Returns {ok, warnings: [(code, message)]} — the plan-of-the-plan check."""
    warnings: list = []

    # frontmatter
    fm = {}
    if text.startswith("---"):
        block = text.split("---")[1]
        for line in block.splitlines():
            m = re.match(r"^(\w+):\s*(\S+)", line)
            if m:
                fm[m.group(1)] = m.group(2)
    for field in ("status", "created", "slug"):
        if field not in fm:
            warnings.append(("no-frontmatter-" + field,
                             f"frontmatter missing `{field}` — plan-reconcile and the ledger need it"))

    # required sections
    def has_section(name_fragment: str) -> bool:
        return any(name_fragment in h for h in re.findall(r"^##\s+(.+)$", text, re.M))

    for fragment, code, why in (
        ("Acceptance criteria", "no-criteria-section", "criteria checkboxes define C<i> for every gate"),
        ("Approach", "no-approach-section", "arch-gate reads claims from the Approach"),
        ("Linked artifacts", "no-linked-artifacts", "docs-gate consumes this section"),
        ("Verification", "no-verification", "evidence-gate expects evidence per criterion"),
        ("Out of scope", "no-out-of-scope", "scope-out rationale feeds dispositions"),
    ):
        if not has_section(fragment):
            warnings.append((code, f"missing `## {fragment}` — {why}"))

    # criteria parseability: single-line checkboxes (order = C<i> identity)
    criteria = re.findall(r"^\s*[-*]\s+\[[ xX]\]\s+.*$", text, re.M)
    if criteria:
        multi = [c for c in text.splitlines()
                 if re.match(r"^\s*[-*]\s+\[[ xX]\]\s*$", c)]
        if multi:
            warnings.append(("empty-checkbox", f"{len(multi)} empty checkbox line(s) — criteria must be single-line statements"))

    # ux contract references resolve
    for ref in re.findall(r"docs/ux/([\w-]+)\.md", text):
        if not (Path("docs/ux") / f"{ref}.md").exists() and \
           not (Path.cwd() / "docs/ux" / f"{ref}.md").exists():
            warnings.append(("ux-contract-missing",
                             f"referenced ux contract docs/ux/{ref}.md not found in this repo"))

    return {"ok": not warnings, "warnings": warnings}


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

    # structural preflight — mechanical, before any model call: the plan must
    # carry what the whole toolchain consumes (criteria order = C<i>, sections
    # per consumer, resolvable ux contract references)
    structure = _plan_structure(text)
    for code, msg in structure["warnings"]:
        print(f"    [STRUCTURE] {msg} ({code})")
    if structure["warnings"]:
        print(f"  structure: {len(structure['warnings'])} warning(s) — these starve downstream tools")

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
        "structure_warnings": [code for code, _msg in structure["warnings"]],
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


def cmd_judge(args: argparse.Namespace) -> int:
    """
    Generic judgment op: ad-hoc heads over one text, classified through sys1
    and logged as a store row. The shared judging surface for classifications
    the named gates do not carry — a workflow runtime's dispatch gate, a
    swarm's atomicity check, a one-off probe. Named gates stay as their own
    ops (and may be promoted from a judge pattern once it earns a name); this
    is the catch-all beneath them, and every row it logs is a calibration row
    like any other. Stdout is one JSON line the caller parses; the human
    summary goes to stderr.
    """
    if sys1 is None:
        print("error: judge requires sys1", file=sys.stderr)
        return EXIT_ERROR
    try:
        specs = json.loads(args.heads)
    except json.JSONDecodeError as e:
        print(f"error: --heads is not valid JSON: {e}", file=sys.stderr)
        return EXIT_ERROR
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list) or not specs:
        print("error: --heads must be a non-empty JSON array of head specs", file=sys.stderr)
        return EXIT_ERROR

    heads: list = []
    for i, h in enumerate(specs):
        if not isinstance(h, dict) or not str(h.get("task") or "").strip():
            print(f"error: head {i} needs a 'task' string", file=sys.stderr)
            return EXIT_ERROR
        head_id = str(h.get("id") or f"h{i}")
        kind = str(h.get("kind") or "choice").lower()
        try:
            if kind == "noul":
                heads.append(sys1.make_noul(str(h["task"]), id=head_id,
                                            descriptions=h.get("descriptions")))
            else:
                labels = h.get("labels")
                if not isinstance(labels, list) or not labels:
                    print(f"error: choice head '{head_id}' needs 'labels'", file=sys.stderr)
                    return EXIT_ERROR
                heads.append(sys1.make_choice(str(h["task"]), [str(l) for l in labels],
                                              id=head_id, descriptions=h.get("descriptions")))
        except Exception as e:  # a malformed head is a caller bug, not a crash
            print(f"error: head '{head_id}' rejected by sys1: {e}", file=sys.stderr)
            return EXIT_ERROR

    if args.text_file:
        try:
            state = Path(args.text_file).read_text()
        except OSError as e:
            print(f"error: cannot read --text-file: {e}", file=sys.stderr)
            return EXIT_ERROR
    else:
        state = args.text or ""
    state = state[: args.max_chars]

    cfg = load_config(None)
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]
    task = sys1.types.Task(id=args.task_id, heads=heads,
                           description=args.description or "ad-hoc judgment (dev-decisions judge)")
    try:
        chain = _sys1_chain(provider) if provider != "auto" else (
            sys1.routing.route_decision(task, state, cfg)[0] or _sys1_chain("jev"))
        result = sys1.classify(chain, task, state, cfg=cfg, log=False)
    except Exception as e:
        print(f"error: classification failed: {e}", file=sys.stderr)
        return EXIT_ERROR

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

    log_record({
        "op": "judge",
        "task": args.task_id,
        "provider": used_provider,
        "input_sha256": _sha16(state),
        "chain": chain,
        "head_ids": [h.id for h in heads],
        "heads": answers,
        "latency_ms": result.latency_ms,
        "telemetry": result.telemetry.get(used_provider, {}),
    })
    print(f"judged '{args.task_id}' via {used_provider} in {result.latency_ms} ms "
          f"({len(heads)} head{'s' if len(heads) != 1 else ''})", file=sys.stderr)
    for h in heads:
        a = answers.get(h.id) or {}
        if a.get("label"):
            conf = a.get("confidence")
            print(f"    {h.id}: {a['label']}"
                  f"{f' (conf {conf:.2f})' if isinstance(conf, (int, float)) else ''}", file=sys.stderr)
        elif a.get("noul") is not None:
            print(f"    {h.id}: noul {a['noul']:.2f}", file=sys.stderr)
    print(json.dumps({"ok": True, "task": args.task_id, "provider": used_provider,
                      "answers": answers, "latency_ms": result.latency_ms}))
    return EXIT_OK


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

    # EVAL-ONLY semantic shortlist (--via-semantic): per artifact, keep only
    # the k doc sections nearest its promise before the fan-out. Embeddings
    # propose, sys1 disposes — this filters the state, never the verdicts;
    # C7 requires verdict equality with the unshortlisted baseline on a fixture.
    shortlists: dict[str, set[int]] = {}
    if getattr(args, "via_semantic", False):
        from .judgment import sem1 as _sem1
        from .semantics import SEM1_RAW_TAG, shortlist as _shortlist
        if _sem1 is None:
            print("error: --via-semantic requires sem1 (set DEV_DECISIONS_SEM1_PATH)", file=sys.stderr)
            return EXIT_ERROR
        k = int(getattr(args, "semantic_k", 0) or 4)
        _embed_cache: dict[str, list] = {}

        def _embed(texts: list[str]) -> list:
            key = "|".join(texts)
            if key not in _embed_cache:
                _embed_cache[key] = _sem1.embed(
                    [{"text": x} for x in texts],
                    provider="llama-server", model="embeddinggemma-2-BF16").vectors
            return _embed_cache[key]

        for rel, promise in artifacts:
            if rel in doc_missing:
                continue
            secs = doc_sections[rel]
            if not secs:
                continue
            vecs = _embed([promise, *[b for _h, b in secs]])
            shortlists[rel] = set(_shortlist(vecs[0], vecs[1:], k))
        kept = sum(len(s) for s in shortlists.values())
        total = sum(len(doc_sections.get(r, [])) for r in shortlists)
        print(f"  via-semantic [{SEM1_RAW_TAG}]: shortlisted {kept}/{total} sections (k={k})")

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
        _keep = shortlists.get(rel)
        options = {f"d{j}.{s}": h for s, (h, _b) in enumerate(secs)
                   if _keep is None or s in _keep}
        options["none"] = "No section in this document covers the promise."
        cov_heads.append(sys1.make_choice(
            f"Which section of {rel} covers the promised update?",
            list(options.keys()), id=f"a{i}_where", descriptions=options))
    parts = [f"Documentation coverage check: {len(artifacts)} promised updates (A0..A{len(artifacts) - 1}) against their documents' sections (d<j>.<s>)."]
    for i, (rel, promise) in enumerate(artifacts):
        parts.append(f"A{i} -> {rel}: {promise}")
    for j, rel in enumerate(doc_rels):
        _keep = shortlists.get(rel)
        for s, (h, b) in enumerate(doc_sections[rel]):
            if _keep is not None and s not in _keep:
                continue
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
    if shortlists:
        providers_used = f"sem1_raw+{providers_used}" if providers_used else "sem1_raw"
    log_record({
        "op": "docs-gate",
        "plan": str(plan_path),
        "provider": providers_used,
        "task": "docs_gate",
        "input_sha256": _sha16(cov_state),
        "heads": answers,
        "artifacts": len(artifacts),
        "via_semantic": {r: sorted(s) for r, s in sorted(shortlists.items())} if shortlists else None,
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


def _daily_series(graded_rows: list, days: int = 30) -> list:
    """Graded rows bucketed by day: [{date, n, accuracy}] sorted by date,
    capped to the last `days` buckets WITH data. Rows without a usable ts or
    actual are excluded (they cannot sit on a time axis)."""
    buckets: dict = {}
    for r in graded_rows:
        ts = str(r.get("ts") or "")
        if len(ts) < 10 or r.get("actual") is None or r.get("predicted") is None:
            continue
        day = ts[:10]
        b = buckets.setdefault(day, {"n": 0, "ok": 0})
        b["n"] += 1
        b["ok"] += 1 if str(r["predicted"]) == str(r["actual"]) else 0
    out = [{"date": d, "n": b["n"], "accuracy": round(b["ok"] / b["n"], 3)}
           for d, b in sorted(buckets.items())]
    return out[-days:]


def _trend(daily: list, *, min_side: int = 3, min_total: int = 6) -> dict:
    """Early-vs-late accuracy halves (split by graded-row count). direction:
    up/down at +/-0.05, flat between, None while gathering."""
    total = sum(b["n"] for b in daily)
    if len(daily) < 2 or total < min_total:
        return {"direction": None, "reason": "gathering"}
    half, acc = total / 2, 0
    split = 1
    for i, b in enumerate(daily):
        acc += b["n"]
        if acc >= half and i + 1 < len(daily):
            split = i + 1
            break
    early, late = daily[:split], daily[split:]
    en, ln = sum(b["n"] for b in early), sum(b["n"] for b in late)
    if en < min_side or ln < min_side:
        return {"direction": None, "reason": "gathering"}
    em = sum(b["accuracy"] * b["n"] for b in early) / en
    lm = sum(b["accuracy"] * b["n"] for b in late) / ln
    delta = lm - em
    direction = "up" if delta >= 0.05 else ("down" if delta <= -0.05 else "flat")
    return {"direction": direction, "early_acc": round(em, 3), "late_acc": round(lm, 3),
            "early_n": en, "late_n": ln, "delta": round(delta, 3)}


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
                    "ts": r.get("ts"),
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

    # jaggedness watch: across draw-carrying event rows, how often do heads
    # come back unstable? (2026-10-02: single-draw plan-gate flags proved
    # jagged; consistency merging now records per-head stability)
    jag: dict = {}
    for f in sorted(LOG_DIR.glob("20*/*/*/events.jsonl"), reverse=True)[:60]:
        try:
            for line in f.read_text().splitlines()[-400:]:
                r = json.loads(line)
                heads = r.get("heads")
                if not isinstance(heads, dict):
                    continue
                # two shapes occur: provider-nested {prov: {head: answer}}
                # and flat {head: answer} (plan-gate logs flat)
                for prov, hd in heads.items():
                    if not isinstance(hd, dict):
                        continue
                    if any(isinstance(a, dict) and "unstable" in a for a in hd.values()):
                        inner = hd.items()
                        task = str(r.get("task") or r.get("op"))
                    else:
                        inner = [(prov, hd)]
                        task = str(r.get("task") or r.get("op"))
                    for hid, a in inner:
                        if isinstance(a, dict) and "unstable" in a:
                            j = jag.setdefault((task, str(prov) if len(inner) > 1 or prov != hid else "flat"),
                                               {"drawn": 0, "unstable": 0})
                            j["drawn"] += 1
                            j["unstable"] += 1 if a.get("unstable") else 0
        except Exception:
            continue
    jaggedness = [{"task": k[0], "provider": k[1],
                   "heads_drawn": v["drawn"],
                   "unstable_rate": round(v["unstable"] / v["drawn"], 3) if v["drawn"] else None}
                  for k, v in sorted(jag.items()) if v["drawn"]]

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
        daily = _daily_series(graded)
        report.append({"task": task, "head": head_id, "provider": provider,
                       "n": n, "accuracy": round(correct / n, 3) if n else None,
                       "span": span, "curve": curve, "daily": daily,
                       "trend": _trend(daily), "qualifies_for_fit": qualifies})

    if args.format == "json":
        print(json.dumps({"sources": {"dev-decisions": n_dd, "sys1": n_sys1},
                          "groups": report, "jaggedness": jaggedness}, indent=2))
        return EXIT_OK

    arrow = {"up": "\u25b2", "down": "\u25bc", "flat": "\u00b7", None: ""}
    print(f"Calibration: {n_dd} rows from dev-decisions, {n_sys1} from the sys1 store")
    if jaggedness:
        print("jaggedness (unstable draw rate):")
        for j in jaggedness:
            print(f"  {j['task']} / {j['provider']}: {j['unstable_rate']} over {j['heads_drawn']} heads")
    print(f"{'task':<22} {'head':<24} {'provider':<12} {'n':>4} {'acc':>6} {'span':>5}  trend  fit?")
    for g in report:
        t = g.get("trend") or {}
        suffix = ""
        if t.get("direction"):
            suffix = f"{arrow[t['direction']]} {t.get('early_acc')} -> {t.get('late_acc')}"
        elif t.get("reason") == "gathering":
            suffix = "gathering"
        print(f"{g['task']:<22} {g['head']:<24} {g['provider']:<12} {g['n']:>4} "
              f"{str(g['accuracy']):>6} {g['span']:>5}  {suffix:<14} {'YES' if g['qualifies_for_fit'] else ''}")
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
    "ux-gate": ("ux-gate", "target"),
    "zcode-gate": ("zcode-gate", "command"),
    "judge": ("judge", "task"),
    # tabular lane gates (2026-10-07 plan): target = table path / bench name / fleet
    "history-gate": ("history-gate", "target"),
    "budget-gate": ("budget-gate", "target"),
    "fleet-anomaly": ("fleet-anomaly", "target"),
    "override-prior": ("override-prior", "target"),
    "risk-prior": ("risk-prior", "target"),
    # media generation lane gate (2026-10-08 plan): target = script or request
    "media-gate": ("media-gate", "target"),
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


