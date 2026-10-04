"""ux + uc corpora, triage, changelog."""

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

from .config import load_config, log_record
from .judgment import _SYS1_PROVIDER_REMAP, _call_jev_raw, _call_local_provider, _call_modernbert_provider, _call_openai_compatible, _env_key, _parse_decide_response, _parse_jev_response, _sys1_chain, _sys1_classify_single, get_task_heads, sys1
from .gitops import get_repo_root, repo_name
from .gates import _call_gh

# section: corpora (moved verbatim from dev_decisions.py)

# ── ux corpus: route contracts, capture verification, gate ───────────────────

_UX_STATE_ALIASES = {"real": "has-data"}


def _parse_ux_contract(path: Path) -> dict:
    """Parse a docs/ux route contract: ## States checkboxes + ## Controls bullets
    + ## Transitions bullets (action -> destination-route)."""
    states, controls, transitions = {}, {}, []
    section = None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            section = ("states" if "States" in line else
                       ("controls" if "Controls" in line else
                        ("transitions" if "Transitions" in line else None)))
            continue
        if section == "transitions":
            m = re.match(r"^\s*[-*]\s+(.+?)\s+->\s+(\S+)\s*$", line)
            if m:
                transitions.append({"action": m.group(1).strip(),
                                    "to": m.group(2).strip().strip("`")})
            continue
        if section == "states":
            m = re.match(r"^\s*[-*]\s+\[[ xX]\]\s+([\w-]+):\s*(.+)$", line)
            if m:
                states[m.group(1)] = m.group(2).strip()
        elif section == "controls":
            m = re.match(r"^\s*[-*]\s+([^:]+):\s*(.+)$", line)
            if m:
                controls[m.group(1).strip()] = m.group(2).strip()
    return {"route": path.stem, "states": states, "controls": controls,
            "transitions": transitions}


def _load_ux_flags(flags_dir: Path) -> dict:
    """{(route, state): flag-report} from the ux-capture kit's *.ux-flag.json.
    Cell state tokens normalize through _UX_STATE_ALIASES (real -> has-data)."""
    out: dict = {}
    if not flags_dir.exists():
        return out
    for f in sorted(flags_dir.glob("*.ux-flag.json")):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        cell = r.get("cell") or ""
        if "/" not in cell:
            continue
        route, state = cell.split("/", 1)
        state = _UX_STATE_ALIASES.get(state, state)
        graded = [fl for fl in r.get("flags", []) if fl.get("flag_correct") is not None]
        out[(route, state)] = {
            "flags": r.get("flags", []),
            "confirmed_drift": [fl for fl in graded
                                if fl["type"] == "implementation-gap" and fl["flag_correct"]],
            "controls_pass": r.get("controls_pass", []),
            "draw_mismatch": r.get("draw_mismatch", False),
            "semantic": r.get("semantic"),
        }
    return out


def cmd_ux_surface(args: argparse.Namespace) -> int:
    """
    Build the UX surface artifact for an app (feed-forward decomposition input
    for design, the plan-surface pattern applied to UI): routes x designed
    states x verification status, merged from docs/ux route contracts and the
    ux-capture kit's graded flag reports. Mechanical: enumeration and merge,
    no model calls — judgment entered through the capture grading.
    """
    ux_docs = Path(args.ux_docs).expanduser()
    if not ux_docs.exists():
        print(f"error: ux docs dir not found: {ux_docs}", file=sys.stderr)
        return EXIT_ERROR
    contracts = [_parse_ux_contract(f) for f in sorted(ux_docs.glob("*.md"))]
    contracts = [c for c in contracts if c["states"]]
    if not contracts:
        print("error: no route contracts with ## States found", file=sys.stderr)
        return EXIT_ERROR
    app_entry = args.entry or args.app
    flags = _load_ux_flags(Path(args.flags_dir).expanduser())

    # flow graph (mechanical): every transition destination must be a
    # contracted route; routes with no inbound transition are orphans; non-leaf
    # routes with no outbound transitions are dead ends. Code decides what is
    # decidable — these never touch a model.
    route_names = {c["route"] for c in contracts}
    undefined, orphans, dead_ends = [], [], []
    outbounds: dict = {}
    for c in contracts:
        for t in c.get("transitions", []):
            if t["to"] not in route_names and t["to"] not in ("external", "exit"):
                undefined.append(f"{c['route']}: {t['action']} -> {t['to']} (no contract)")
            outbounds.setdefault(c["route"], 0)
            outbounds[c["route"]] += 1
    inbounds: dict = {}
    for c in contracts:
        for t in c.get("transitions", []):
            inbounds[t["to"]] = inbounds.get(t["to"], 0) + 1
    for c in contracts:
        if inbounds.get(c["route"], 0) == 0 and c["route"] != app_entry:
            orphans.append(c["route"])
        # dead end: reachable (has inbound) but nothing to leave by
        if inbounds.get(c["route"], 0) > 0 and outbounds.get(c["route"], 0) == 0:
            dead_ends.append(c["route"])

    routes = []
    n_states = n_verified = n_drift = 0
    for c in contracts:
        states_out = []
        for name, desc in c["states"].items():
            rep = flags.get((c["route"], name))
            if rep is None:
                verification, cell_flags = "unverified", []
            else:
                cell_flags = [
                    {"control": fl["control"], "type": fl["type"],
                     "graded": (fl.get("graded") or {}).get("who"),
                     "cause": (fl.get("graded") or {}).get("cause")}
                    for fl in rep["flags"]]
                if rep["confirmed_drift"]:
                    verification = "confirmed-drift"
                    n_drift += len(rep["confirmed_drift"])
                elif cell_flags:
                    verification = "flags-ungraded"
                else:
                    verification = "clean"
                n_verified += 1
            n_states += 1
            confirmed = [
                {"control": fl["control"], "cause": (fl.get("graded") or {}).get("cause")}
                for fl in (rep or {}).get("confirmed_drift", [])]
            states_out.append({"state": name, "designed": desc,
                               "verification": verification, "flags": cell_flags,
                               "confirmed_drift": confirmed})
        routes.append({"route": c["route"], "n_controls": len(c["controls"]),
                       "states": states_out, "transitions": c.get("transitions", [])})

    artifact = {
        "schema": "ux-surface/v1",
        "app": args.app,
        "generated": datetime.now(timezone.utc).isoformat(),
        "routes": routes,
        "flow": {"undefined_destinations": undefined, "orphan_routes": orphans,
                 "dead_end_routes": dead_ends,
                 "entry": app_entry},
        "summary": {"routes": len(routes), "states": n_states,
                    "states_verified": n_verified, "confirmed_drift_flags": n_drift,
                    "flow_problems": len(undefined) + len(orphans) + len(dead_ends)},
    }
    out_dir = SURFACES_DIR / "ux"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.app}.surface.json"
    out_path.write_text(json.dumps(artifact, indent=2))

    print(f"UX surface: {args.app}  ({len(routes)} routes, {n_states} designed states)")
    for r in routes:
        for st in r["states"]:
            mark = {"confirmed-drift": "[DRIFT]", "flags-ungraded": "[UNGRADED]",
                    "clean": "[clean]", "unverified": "[no capture]"}.get(st["verification"], "?")
            print(f"  {r['route']:<12} {st['state']:<10} {mark}")
    flow = artifact["flow"]
    if flow["undefined_destinations"]:
        print("  flow: UNDEFINED destinations:")
        for u in flow["undefined_destinations"]:
            print(f"    {u}")
    if flow["orphan_routes"]:
        print(f"  flow: orphan routes (no inbound): {', '.join(flow['orphan_routes'])}")
    if flow["dead_end_routes"]:
        print(f"  flow: dead ends (no outbound): {', '.join(flow['dead_end_routes'])}")
    print(f"  artifact: {out_path}")
    print(f"  confirmed drift flags: {n_drift}")

    log_record({
        "op": "ux-surface", "app": args.app,
        "task": "ux_surface", "input_sha256": hashlib.sha256(
            json.dumps(artifact, sort_keys=True).encode()).hexdigest()[:16],
        "routes": len(routes), "states": n_states,
        "states_verified": n_verified, "confirmed_drift_flags": n_drift,
        "flow_problems": len(undefined) + len(orphans) + len(dead_ends),
        "artifact": str(out_path), "verdict": "ok",
    })
    return EXIT_OK


def cmd_ux_gate(args: argparse.Namespace) -> int:
    """
    Gate an app's UX surface (feed-back): confirmed drift (human-graded real)
    fails; ungraded flags and unverified states warn; semantic state-match
    stays WARN-only until its floor is fitted. Counting is code.
    """
    surface_path = SURFACES_DIR / "ux" / f"{args.app}.surface.json"
    if args.surface:
        surface_path = Path(args.surface).expanduser()
    if not surface_path.exists():
        print(f"error: no ux surface at {surface_path} — run `dev-decisions ux-surface` first", file=sys.stderr)
        return EXIT_ERROR
    artifact = json.loads(surface_path.read_text())

    drift, ungraded, unverified = [], [], []
    flow = artifact.get("flow") or {}
    for u in flow.get("undefined_destinations", []):
        ungraded.append(f"    [FLOW] {u}")
    if flow.get("orphan_routes"):
        ungraded.append(f"    [FLOW] orphan routes: {', '.join(flow['orphan_routes'])}")
    if flow.get("dead_end_routes"):
        ungraded.append(f"    [FLOW] dead ends: {', '.join(flow['dead_end_routes'])}")
    for r in artifact.get("routes", []):
        for st in r.get("states", []):
            cell = f"{r['route']}/{st['state']}"
            if st["verification"] == "confirmed-drift":
                for fl in st.get("confirmed_drift", []):
                    drift.append(f"    [DRIFT]  {cell}: {fl['control']} "
                                 f"({(fl.get('cause') or '')[:70]})")
            elif st["verification"] == "flags-ungraded":
                ungraded.append(f"    [UNGRADED] {cell}: "
                                + ", ".join(fl["control"] for fl in st["flags"]))
            elif st["verification"] == "unverified":
                unverified.append(f"    [NO CAPTURE] {cell}")

    print(f"UX gate: {args.app}  ({len(artifact.get('routes', []))} routes)")
    for line in drift:
        print(line)
    for line in ungraded:
        print(line)
    for line in unverified:
        print(line)
    gaps = bool(drift)
    verdict = "drift" if drift else ("flags" if (ungraded or unverified) else "pass")
    log_record({
        "op": "ux-gate", "app": args.app, "target": args.app, "task": "ux_gate",
        "input_sha256": hashlib.sha256(surface_path.read_bytes()).hexdigest()[:16],
        "confirmed_drift": len(drift), "ungraded_flags": len(ungraded),
        "drifts": len(drift),
        "unverified_states": len(unverified),
        "verdict": verdict,
    })
    print(f"  verdict: {verdict.upper()}")
    print("  disposition with: dev-decisions disposition ux-gate <app> --status fixed|waived --reason ...")
    return EXIT_WARN if (drift or ungraded or unverified) else EXIT_OK


# ── use-case corpus: issues vs existing functionality vs planned design ──────

_UX_ALIASES = _UX_STATE_ALIASES  # shared alias table convention


def _parse_uc_issues(path: Path) -> list:
    """Issues contract: - [ ] <id>: <statement> under ## Issues."""
    issues, section = [], None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            section = "issues" if "Issues" in line else None
            continue
        if section != "issues":
            continue
        m = re.match(r"^\s*[-*]\s+\[[ xX]\]\s+([\w-]+):\s*(.+?)\s*$", line)
        if m:
            issues.append({"id": m.group(1), "statement": m.group(2)})
    return issues


def _parse_inventory(path: Path, cap: int = 600) -> list:
    """
    Mechanism/behavior inventory from a design or spec document: headings
    with an id prefix ("### F1 — Title" -> id F1) and numbered bold list
    items ("1. **Chats** (...)" -> id m1, m2, ...). Each entry keeps its
    section body (capped) so the coverage judge reads substance, not titles.
    """
    text = path.read_text()
    entries: list = []

    def add(eid, title, body_lines):
        body = " ".join(b.strip() for b in body_lines).strip()
        entries.append({"id": eid, "title": title.strip(), "body": body[:cap]})

    lines = text.splitlines()
    i = 0
    m_counter = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^#{2,3}\s+(?:\*\*)?([FAX]\d+|UC\d+|REQ-?[\w-]+)\b[\s—:-]*(.+?)(?:\*\*)?\s*$", line)
        if m:
            body = []
            j = i + 1
            while j < len(lines) and not re.match(r"^#{2,3}\s", lines[j]):
                body.append(lines[j])
                j += 1
            add(m.group(1), m.group(2), body)
            i = j
            continue
        n = re.match(r"^\s*(\d+)\.\s+\*\*([^*]+)\*\*", line)
        if n:
            m_counter += 1
            body = [line.split("**", 2)[-1]]
            j = i + 1
            while j < len(lines) and not re.match(r"^\s*\d+\.\s+\*\*", lines[j]) and not re.match(r"^#{2,3}\s", lines[j]):
                body.append(lines[j])
                j += 1
            add(f"m{m_counter}", n.group(2), body)
            i = j
            continue
        i += 1
    return entries


def _coverage_fanout(issues: list, inventory: list, label: str, provider: str, cfg: dict, *, cite_floor: float = 0.15) -> dict:
    """
    Citation-forced coverage per issue: one choice head (which inventory ids
    address it, incl. 'none') + one resolve noul (fully resolved by what it
    cites?). Mechanical post-check: cited ids must exist — bad citations are
    'none' with a note (the mention-trap guard).
    """
    ids = [e["id"] for e in inventory]
    desc = {e["id"]: f"{e['title']} — {e['body'][:180]}" for e in inventory}
    heads = []
    for i, iss in enumerate(issues):
        heads.append(sys1.make_choice(
            f"Which {label} capabilities address issue {iss['id']} "
            f"(\"{iss['statement'][:140]}\")? Cite every id that contributes.",
            ids + ["none"], id=f"uc{i}_cite", multi_label=True,
            descriptions=desc))
        heads.append(sys1.make_noul(
            f"If the cited {label} capabilities operate together, is issue {iss['id']} "
            f"fully resolved (not just partially)? Issue: {iss['statement'][:140]}",
            id=f"uc{i}_full"))
    state = "\n\n".join(
        f"{e['id']} — {e['title']}: {e['body']}" for e in inventory)
    task = sys1.types.Task(id=f"uc_coverage_{label.replace(' ', '_')}", heads=heads,
                           description=f"Issue coverage vs {label} inventory")
    chain = _sys1_chain(provider) if provider != "auto" else \
        (sys1.routing.route_decision(task, state, cfg)[0] or ["glide", "drex", "jev"])
    result = sys1.classify(chain, task, state, cfg=cfg, log=True)
    used, answers = chain[0] if chain else "", {}
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        if result.answers.get(mapped):
            answers = result.answers[mapped]
            used = mapped
            break

    per_issue: dict = {}
    id_set = set(ids)
    for i, iss in enumerate(issues):
        cite = answers.get(f"uc{i}_cite") or {}
        full = answers.get(f"uc{i}_full") or {}
        # cite from the PROBABILITY DISTRIBUTION, not the label — jev-family
        # wires ignore multi-label, and the Phase 0 lesson (semantic-find)
        # applies: probabilities carry the full citation set
        probs = {k: float(v) for k, v in (cite.get("probabilities") or {}).items()}
        cited_ids = [k for k, pv in probs.items() if k in id_set and pv >= cite_floor]
        if not cited_ids and cite.get("label") in id_set:
            cited_ids = [cite["label"]]
        bad = []  # probabilities only contain real inventory ids; label checked above
        p_full = full.get("noul") if isinstance(full.get("noul"), (int, float)) else None
        # three degrees: none = nothing validly cited; partial = cited but
        # the resolve noul doubts full resolution; full = cited and confirmed
        if bad or not cited_ids:
            degree = "none"
        elif p_full is None or p_full >= 0.5:
            degree = "full"
        else:
            degree = "partial"
        per_issue[iss["id"]] = {
            "degree": degree, "cited": cited_ids, "bad_citations": bad,
            "resolve_p": round(p_full, 3) if isinstance(p_full, (int, float)) else None,
            "confidence": cite.get("confidence"),
        }
    return {"provider": used, "per_issue": per_issue}


def _quadrant(existing: str, design: str) -> str:
    if existing == "full":
        return "reinvention" if design in ("full", "partial") else "already-solved"
    if existing == "partial":
        return "extension" if design in ("full", "partial") else "residual-gap"
    return "genuine-new" if design in ("full", "partial") else "true-gap"


_QUADRANT_ORDER = ["true-gap", "reinvention", "residual-gap", "extension",
                   "already-solved", "genuine-new"]


def cmd_uc_gate(args: argparse.Namespace) -> int:
    """
    Use-case coverage gate: are issues A..X already covered by existing
    documented functionality, and does the planned design address them?
    Two citation-forced coverage fan-outs (existing FS inventory, planned
    design inventory) assemble the per-issue 2x2: reinvention /
    already-solved / extension / genuine-new / residual-gap / true-gap.
    True gaps and reinventions fail the gate (advisorial exit 1); every
    verdict is a review input — dispositions resolve who was right and
    grade the coverage heads.
    """
    if sys1 is None:
        print("error: uc-gate requires sys1", file=sys.stderr)
        return EXIT_ERROR
    issues_path = Path(args.issues).expanduser()
    fs_path = Path(args.fs).expanduser()
    design_path = Path(args.design).expanduser()
    for pth in (issues_path, fs_path, design_path):
        if not pth.exists():
            print(f"error: not found: {pth}", file=sys.stderr)
            return EXIT_ERROR
    issues = _parse_uc_issues(issues_path)
    if not issues:
        print("error: no issues found (## Issues with '- [ ] <id>: statement' lines)", file=sys.stderr)
        return EXIT_ERROR
    fs_inv = _parse_inventory(fs_path)
    design_inv = _parse_inventory(design_path)
    if not fs_inv or not design_inv:
        print("error: inventory parse produced no entries", file=sys.stderr)
        return EXIT_ERROR

    cfg = load_config(None)
    provider = getattr(args, "provider", None) or cfg["classify"]["provider"]
    print(f"UC gate: {issues_path.name}  ({len(issues)} issues, "
          f"{len(fs_inv)} FS entries, {len(design_inv)} design mechanisms)")

    print(f"  coverage vs existing functionality ({len(fs_inv)} entries) ...")
    ex = _coverage_fanout(issues, fs_inv, "existing", provider, cfg,
                          cite_floor=float(getattr(args, "cite_floor", 0.15)))
    print(f"  coverage vs planned design ({len(design_inv)} mechanisms) ...")
    de = _coverage_fanout(issues, design_inv, "design", provider, cfg,
                          cite_floor=float(getattr(args, "cite_floor", 0.15)))

    rows = []
    counts: dict = {}
    for iss in issues:
        iid = iss["id"]
        exd = ex["per_issue"].get(iid, {"degree": "none"})
        ded = de["per_issue"].get(iid, {"degree": "none"})
        q = _quadrant(exd["degree"], ded["degree"])
        counts[q] = counts.get(q, 0) + 1
        rows.append({"issue": iid, "statement": iss["statement"], "existing": exd,
                     "design": ded, "quadrant": q})

    order = {q: i for i, q in enumerate(_QUADRANT_ORDER)}
    rows.sort(key=lambda r: order.get(r["quadrant"], 99))

    print("  per issue:")
    for r in rows:
        print(f"    [{r['quadrant']:<13}] {r['issue']}: {r['statement'][:90]}")
        print(f"        existing={r['existing']['degree']} (cited {', '.join(r['existing']['cited']) or '-'})"
              f"  design={r['design']['degree']} (cited {', '.join(r['design']['cited']) or '-'})")

    out = SURFACES_DIR / "uc" / f"{issues_path.stem}.uc.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema": "uc-coverage/v1", "issues": str(issues_path),
        "fs": str(fs_path), "design": str(design_path),
        "providers": {"existing": ex["provider"], "design": de["provider"]},
        "rows": rows}, indent=2))
    print(f"  report: {out}")

    hard = counts.get("true-gap", 0) + counts.get("reinvention", 0)
    log_record({
        "op": "uc-gate", "target": str(issues_path), "app": args.app or issues_path.stem,
        "task": "uc_coverage", "input_sha256": hashlib.sha256(
            (issues_path.read_text() + fs_path.read_text() + design_path.read_text()).encode()
        ).hexdigest()[:16],
        "issues": len(issues), "hard": hard,
        "counts": counts,
        "providers": f"fs={ex['provider']},design={de['provider']}",
        "report": str(out),
        "verdict": "gaps" if hard else "pass",
        "drifts": hard,
    })
    print(f"  verdict: {hard} hard finding(s) "
          f"(true-gaps {counts.get('true-gap', 0)}, reinventions {counts.get('reinvention', 0)})")
    return EXIT_WARN if hard else EXIT_OK


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


