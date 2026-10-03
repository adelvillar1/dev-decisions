#!/usr/bin/env python3
"""Self-tests for the 2026-10-02 calibration-loop fixes.

stdlib unittest, no pytest dependency (repo convention). Run:

    python3 scripts/selftest.py

Covers the pieces the grading session proved load-bearing:
  - _strip_inert_spans: the 8 graded zcode-gate false positives (quoted text,
    commit messages, heredocs) must produce NO destructive hit; real
    destructive commands in command position MUST still hit.
  - _merge_draws: plan-gate self-consistency merging (mean across draws,
    unstable when draws straddle the cut).
  - detect_task_from_diff: routes on changed PATHS (the old body-text scan
    sent any diff mentioning '.md' to docs_drift).
  - _disposition_feedback_rows: fixed/waived keep actual=predicted,
    overridden inverts yes/no heads and nulls choice heads.
  - _daily_series/_trend: calibration drift bucketing and the early-vs-late
    direction guard (gathering until both halves have >= 3 rows).
  - _parse_ux_contract/_load_ux_flags: route-contract parsing and capture
    cell normalization (real -> has-data) for the ux corpus.
  - uc-gate: issues/inventory parsing (F-heading + numbered-bold styles),
    quadrant classification (all six outcomes), bad-citation guard.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_spec = importlib.util.spec_from_file_location("dev_decisions_under_test", _HERE.parent / "dev_decisions.py")
dd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dd)


def _hits(cmd: str) -> list:
    skeleton = dd._strip_inert_spans(cmd)
    return [p for p in dd._DESTRUCTIVE_PATTERNS if re.search(p, skeleton, re.IGNORECASE)]


# the graded false positives (2026-10-02): pattern text inside INERT spans
GRADED_FP_COMMANDS = [
    "cd X && sed -n '76,88p' auth.test.ts; echo '=== drop/truncate in api tests? ==='; "
    "grep -rln 'dropTable\\|DROP TABLE\\|truncate' apps",
    "git merge-base --is-ancestor 36850b0 e33485f && echo 'fast-forward: yes (no force needed)'",
    "ditto ~/.second-brain ~/Projects/second-brain && diff ~/.second-brain/db.py ~/Projects/second-brain/db.py",
    "cd repo && python3 -m py_compile app.py && echo '=== zcode-gate: destructive + irreversible test ==='",
    "git commit --no-verify -m x",
    "cd repo && git add scripts/dev_decisions.py SKILL.md && "
    "git commit -m 'HITL policy: disposition command + destructive-irreversible hard-block'",
    "cd /tmp/x && python3 - <<'EOF'\nimport shutil\nprint('rm -rf lives in this quoted text')\nEOF",
    "cd repo && python3 -m py_compile app.py && find logs -name '*.jsonl' | tail -2; "
    "echo '--- block test: git push --force origin main ---'",
]

LEGIT_DESTRUCTIVE = [
    "git push --force origin main",
    "rm -rf /tmp/test",
    "kubectl delete pod x",
    "cd repo && git push --force-with-lease origin main",
    "psql -c 'DROP TABLE users'",
]


class TestStripInertSpans(unittest.TestCase):
    def test_graded_false_positives_are_clean(self):
        for cmd in GRADED_FP_COMMANDS:
            self.assertEqual(_hits(cmd), [], f"false positive still trips: {cmd[:70]}")

    def test_legit_destructive_commands_still_hit(self):
        for cmd in LEGIT_DESTRUCTIVE:
            self.assertTrue(_hits(cmd), f"legit destructive missed: {cmd[:70]}")

    def test_command_substitutions_stay_in_skeleton(self):
        # $( ) EXECUTES — the pattern must still be found inside it
        cmd = "echo $(git push --force origin main)"
        self.assertTrue(_hits(cmd))

    def test_quoted_spans_replaced_not_joined(self):
        # stripping must not splice 'push' + '--force' across a removed span
        cmd = "echo 'push'; echo 'safe --force text'"
        self.assertEqual(_hits(cmd), [])


class TestMergeDraws(unittest.TestCase):
    def test_noul_mean_and_unstable_on_straddle(self):
        draws = [{"h": {"type": "noul", "noul": 0.9}},
                 {"h": {"type": "noul", "noul": 0.4}},
                 {"h": {"type": "noul", "noul": 0.8}}]
        merged = dd._merge_draws(draws)["h"]
        self.assertAlmostEqual(merged["noul"], 0.7, places=3)
        self.assertTrue(merged["unstable"])
        self.assertEqual(merged["draws"], 3)

    def test_noul_consistent_is_stable(self):
        draws = [{"h": {"type": "noul", "noul": p}} for p in (0.95, 0.93, 0.94)]
        merged = dd._merge_draws(draws)["h"]
        self.assertAlmostEqual(merged["noul"], 0.94, places=3)
        self.assertFalse(merged["unstable"])

    def test_choice_majority(self):
        draws = [{"h": {"type": "choice", "label": "a", "confidence": 0.8}},
                 {"h": {"type": "choice", "label": "a", "confidence": 0.7}},
                 {"h": {"type": "choice", "label": "b", "confidence": 0.6}}]
        merged = dd._merge_draws(draws)["h"]
        self.assertEqual(merged["label"], "a")
        self.assertTrue(merged["unstable"])
        self.assertAlmostEqual(merged["confidence"], 0.7, places=3)

    def test_declined_draws_do_not_poison(self):
        draws = [{"h": {"_declined": "no-answer", "confidence": 0.0}},
                 {"h": {"type": "noul", "noul": 0.9}},
                 {"h": {"type": "noul", "noul": 0.9}}]
        merged = dd._merge_draws(draws)["h"]
        self.assertAlmostEqual(merged["noul"], 0.9, places=3)
        self.assertFalse(merged.get("unstable"))


class TestDiffRouting(unittest.TestCase):
    def test_paths_extracted_from_headers(self):
        diff = "diff --git a/docs/x.md b/docs/x.md\nline\ndiff --git a/src/y.py b/src/y.py\n"
        self.assertEqual(dd._diff_paths(diff), ["docs/x.md", "src/y.py"])

    def test_docs_only_paths_route_to_docs_drift(self):
        diff = "diff --git a/README.md b/README.md\n- old\n+ new\n"
        self.assertEqual(dd.detect_task_from_diff(diff), "docs_drift")

    def test_code_diff_mentioning_md_is_change(self):
        # the old body-text scan routed this to docs_drift (graded quirk)
        diff = "diff --git a/src/app.py b/src/app.py\n- load('.md')\n+ load('.markdown')\n"
        self.assertEqual(dd.detect_task_from_diff(diff), "change")

    def test_dep_manifest_path_routes_to_deps_risk(self):
        diff = "diff --git a/web/package.json b/web/package.json\n- x\n+ y\n"
        self.assertEqual(dd.detect_task_from_diff(diff), "deps_risk")


class TestDispositionFeedbackRows(unittest.TestCase):
    ROW = {"input_sha256": "abc123", "task": "plan_gate", "provider": "jev",
           "heads": {"jev": {"c0_covered": {"type": "noul", "noul": 0.3},
                             "c1_verifiable": {"type": "choice", "label": "unverifiable",
                                               "confidence": 0.6}}}}

    def test_fixed_keeps_predicted(self):
        rows = dd._disposition_feedback_rows("fixed", self.ROW, "reworked")
        by_head = {r["head_id"]: r for r in rows}
        self.assertEqual(by_head["c0_covered"]["actual"], "no")
        self.assertEqual(by_head["c1_verifiable"]["actual"], "unverifiable")

    def test_overridden_inverts_noul_nulls_choice(self):
        rows = dd._disposition_feedback_rows("overridden", self.ROW, "gate wrong")
        by_head = {r["head_id"]: r for r in rows}
        self.assertEqual(by_head["c0_covered"]["actual"], "yes")
        self.assertIsNone(by_head["c1_verifiable"]["actual"])

    def test_row_without_sha_is_skipped(self):
        self.assertEqual(dd._disposition_feedback_rows("fixed", {"heads": {}}, None), [])


class TestDailySeries(unittest.TestCase):
    def test_buckets_two_days_with_accuracy(self):
        rows = [{"ts": "2026-10-01T10:00:00+00:00", "predicted": "yes", "actual": "yes"},
                {"ts": "2026-10-01T11:00:00+00:00", "predicted": "yes", "actual": "no"},
                {"ts": "2026-10-02T10:00:00+00:00", "predicted": "yes", "actual": "yes"},
                {"ts": "2026-10-02T11:00:00+00:00", "predicted": "yes", "actual": "yes"}]
        daily = dd._daily_series(rows)
        self.assertEqual([b["date"] for b in daily], ["2026-10-01", "2026-10-02"])
        self.assertEqual(daily[0]["accuracy"], 0.5)
        self.assertEqual(daily[1]["accuracy"], 1.0)

    def test_rows_without_actual_or_ts_excluded(self):
        rows = [{"ts": "2026-10-01T10:00:00+00:00", "predicted": "yes", "actual": None},
                {"predicted": "yes", "actual": "yes"}]
        self.assertEqual(dd._daily_series(rows), [])

    def test_days_cap_keeps_latest(self):
        rows = [{"ts": f"2026-09-{str(d).zfill(2)}T10:00:00+00:00", "predicted": "a",
                 "actual": "a"} for d in range(1, 11)]
        daily = dd._daily_series(rows, days=5)
        self.assertEqual(len(daily), 5)
        self.assertEqual(daily[-1]["date"], "2026-09-10")


class TestTrend(unittest.TestCase):
    def test_up_when_late_improves(self):
        daily = [{"date": "2026-10-01", "n": 3, "accuracy": 0.5},
                 {"date": "2026-10-02", "n": 3, "accuracy": 0.9}]
        t = dd._trend(daily)
        self.assertEqual(t["direction"], "up")
        self.assertEqual((t["early_n"], t["late_n"]), (3, 3))

    def test_gathering_until_both_halves_have_three(self):
        t = dd._trend([{"date": "2026-10-01", "n": 2, "accuracy": 0.5},
                       {"date": "2026-10-02", "n": 2, "accuracy": 1.0}])
        self.assertIsNone(t["direction"])
        self.assertEqual(t["reason"], "gathering")

    def test_flat_inside_band(self):
        daily = [{"date": "2026-10-01", "n": 3, "accuracy": 0.8},
                 {"date": "2026-10-02", "n": 3, "accuracy": 0.82}]
        self.assertEqual(dd._trend(daily)["direction"], "flat")


class TestUxCorpus(unittest.TestCase):
    def test_parse_ux_contract(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "gates.md"
            f.write_text("# UX contract\n\n## States\n"
                         "- [ ] has-data: open inbox items present\n"
                         "- [ ] sparse: empty states render\n\n"
                         "## Controls (has-data)\n"
                         "- header nav: chats, knowledge\n"
                         "- stat cards row: open, blocks\n")
            c = dd._parse_ux_contract(f)
            self.assertEqual(c["route"], "gates")
            self.assertEqual(c["states"]["sparse"], "empty states render")
            self.assertIn("stat cards row", c["controls"])

    def test_transitions_and_flow(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "index.md").write_text(
                "# UX contract\n## States\n- [ ] has-data: cockpit populated\n\n"
                "## Transitions\n- open gates -> `gates`\n")
            (Path(td) / "gates.md").write_text(
                "# UX contract\n## States\n- [ ] has-data: inbox\n- [ ] sparse: empty\n\n"
                "## Transitions\n- open summary -> `summary`\n")
            (Path(td) / "reports.md").write_text(
                "# UX contract\n## States\n- [ ] has-data: conflicts\n")
            contracts = [dd._parse_ux_contract(Path(td) / f"{n}.md")
                         for n in ("index", "gates", "reports")]
            route_names = {c["route"] for c in contracts}
            undefined, outbounds, inbounds = [], {}, {}
            for c in contracts:
                for t in c.get("transitions", []):
                    if t["to"] not in route_names:
                        undefined.append(f"{c['route']}: {t['action']} -> {t['to']}")
                    outbounds[c["route"]] = outbounds.get(c["route"], 0) + 1
                    inbounds[t["to"]] = inbounds.get(t["to"], 0) + 1
            orphans = [c["route"] for c in contracts
                       if inbounds.get(c["route"], 0) == 0 and c["route"] != "index"]
            # dead end: reachable but nothing to leave by (no state-count guard)
            dead = [c["route"] for c in contracts
                    if inbounds.get(c["route"], 0) > 0 and outbounds.get(c["route"], 0) == 0]
            self.assertEqual(undefined, ["gates: open summary -> summary"])
            self.assertEqual(orphans, ["reports"])
            self.assertEqual(dead, [])  # reports is unreachable: orphan, not dead end

    def test_flags_normalization_and_drift(self):
        with tempfile.TemporaryDirectory() as td:
            flags_dir = Path(td)
            (flags_dir / "gates.ux-flag.json").write_text(json.dumps({
                "cell": "gates/real", "flags": [
                    {"control": "trust stats", "type": "implementation-gap",
                     "flag_correct": True, "graded": {"who": "spec", "cause": "drift"}},
                    {"control": "nav", "type": "perception",
                     "flag_correct": False, "graded": {"who": "dom"}},
                ], "controls_pass": ["nav"], "draw_mismatch": False}))
            flags = dd._load_ux_flags(flags_dir)
            rep = flags[("gates", "has-data")]  # 'real' normalized
            self.assertEqual(len(rep["confirmed_drift"]), 1)
            self.assertEqual(rep["confirmed_drift"][0]["control"], "trust stats")


class TestUcGate(unittest.TestCase):
    def test_parse_issues_contract(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "uc.md"
            f.write_text("# use case\n\n## Issues\n"
                         "- [ ] A: history exceeds memory\n"
                         "- [ ] web-ux: portal needs a timeline\n\n"
                         "## Context\nsome prose\n")
            issues = dd._parse_uc_issues(f)
            self.assertEqual([i["id"] for i in issues], ["A", "web-ux"])
            self.assertEqual(issues[0]["statement"], "history exceeds memory")

    def test_inventory_heading_style(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "fs.md"
            f.write_text("# FS\n### F1 — Full inventory\nbody one.\n"
                         "### F2 — Drex classification\nbody two.\n")
            inv = dd._parse_inventory(f)
            self.assertEqual([e["id"] for e in inv], ["F1", "F2"])
            self.assertIn("body one", inv[0]["body"])

    def test_inventory_numbered_bold_style(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "plan.md"
            f.write_text("## Approach\n1. **Chats** canonical stores per harness\n"
                         "   resume via JSONL\n2. **Tower** dashboard\n")
            inv = dd._parse_inventory(f)
            self.assertEqual([e["id"] for e in inv], ["m1", "m2"])
            self.assertEqual(inv[0]["title"], "Chats")

    def test_quadrant_table(self):
        q = dd._quadrant
        self.assertEqual(q("full", "full"), "reinvention")
        self.assertEqual(q("full", "partial"), "reinvention")
        self.assertEqual(q("full", "none"), "already-solved")
        self.assertEqual(q("partial", "full"), "extension")
        self.assertEqual(q("partial", "none"), "residual-gap")
        self.assertEqual(q("none", "full"), "genuine-new")
        self.assertEqual(q("none", "partial"), "genuine-new")
        self.assertEqual(q("none", "none"), "true-gap")

    def test_coverage_bad_citation_is_none(self):
        # simulate the fanout's citation post-check via the per-issue logic:
        # cited ids not in the inventory force degree=none
        cited = ["F1", "ghost"]
        ids = ["F1", "F2"]
        bad = [c for c in cited if c not in ids]
        cited = [c for c in cited if c in ids]
        p_full = 0.9
        degree = "full" if cited and p_full >= 0.5 else "none"
        if bad:
            degree = "none"
        self.assertEqual(degree, "none")
        self.assertEqual(bad, ["ghost"])


class TestFeedbackRowShape(unittest.TestCase):
    def test_write_feedback_adds_head_id(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old_log = dd.LOG_DIR
            dd.LOG_DIR = Path(td)
            try:
                dd._write_feedback("abc", "t", "jev", "yes", note="n", head_id="h0")
                rows = [json.loads(ln) for ln in
                        (Path(td) / "feedback" / "feedback.jsonl").read_text().splitlines()]
                self.assertEqual(rows[0]["head_id"], "h0")
                self.assertEqual(rows[0]["label"], "yes")
            finally:
                dd.LOG_DIR = old_log


if __name__ == "__main__":
    unittest.main(verbosity=2)
