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
"""

from __future__ import annotations

import importlib.util
import json
import re
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
