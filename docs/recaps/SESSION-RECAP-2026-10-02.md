# Session recap — 2026-10-02 (2): gates calibration loop, plan-surface, ux corpus, uc-gate

**Scope**: dev-decisions main (merged from `plan-surface-command`); paired sessions touched sys1 (`feature/plan-surface`) and second-brain. This session completed the calibration loop for the whole gate family, shipped the plan-surface/ux/uc corpora, and built the tower surfaces for all of it.

## What shipped (chronological)

- **Grading session (the day's pivot)**: 62 production feedback rows written by hand — safety 48, plan_gate 15, docs_drift 2. Findings: zcode-gate destructive regex had 8 false positives from quoted-text matches (2 at block severity); plan-gate was jagged (3 runs, 3 different flag sets, all false positives on a thorough plan); docs-drift flagged a docs-only commit. Every fix below is evidence-driven from this grading.
- **zcode-gate regex fix**: destructive patterns now match an inert-stripped command skeleton (quoted spans + heredoc bodies removed, interpreter-flag exception for `psql -c '...'`), `--no-verify` demoted to advisory `hook_bypass`, benign commands log `verdict: clean` true-negative rows. All 8 graded FPs clean; force-push/DROP TABLE still block.
- **Log enrichment**: gate rows (zcode-gate, plan-gate, docs-gate, arch-gate, evidence-gate) carry `input_sha256` + per-head `heads` (+ `patterns_hit`, `drifts` int, `unstable`, `claim_texts`), so grading sessions join cleanly.
- **Dispositions write their own calibration rows**: disposition pairs with the matched gate event (30-day window) — fixed/waived grade the prediction correct, overridden inverts.
- **plan-gate self-consistency**: `--draws 3` merges draws (mean/stdev/unstable per head); the jaggedness finding is now measured per run.
- **Mechanical pushdowns**: task routing reads changed PATHS from diff headers (not body text); all-docs diffs force drift to no in code.
- **plan-surface + plan-reconcile**: the ux/plan layered-precompute corpus (see sys1 recap for the module). Live trial on hitl-gate-panel; `plan-reconcile` grades a surface against actual commits.
- **calibration command**: per-head graded counts, accuracy, span, jaggedness (unstable draw rate), floor-fit readiness (≥20 rows spanning ≥0.4). Live: 618 graded rows, **0 heads fit-ready yet**.
- **uc-gate**: issues vs existing functionality vs planned design — the 2×2 coverage matrix with citation-forced coverage heads (cite from probability distributions; bad citations = none). Probe on control-tower-foundation: **citation precision 12/12**, quadrant 0/6 — a documented retroactive artifact (the FS post-dates the plan, so "existing" reads the plan's own output). Valid mode is design-time, before the FS absorbs the new functionality. Deployment constraint recorded in SKILL.md.
- **dev-decisions merged to main** (was `plan-surface-command`).

## Tests

`scripts/selftest.py` created (this repo's first tests): 24+ stdlib-unittest cases — zcode-gate skeleton vs all 8 graded FPs, draw merging, path routing, disposition feedback pairing, uc parsing/quadrants/citations. All green.

## Notable / gotchas

- `~/.config/dev-decisions/env` needs `set -a` before sourcing — vars are not exported, and unsourced keys silently produce zero-provider answers.
- `sys1.classify` skips JSONL logging entirely when every provider fails — empty answers mean provider-resolution failure; check telemetry, not logs.
- TYPESAFE_API_KEY lives in `~/.config/dev-decisions/env`, not zshrc; overwriting it from the wrong file broke semantic calls once.
- uc-gate state-name regex must accept hyphens (`has-data`).

## Open / next

- **User disposition pending**: conflicts.html trust/coverage → RESOLVED (overridden; contract amended; fresh captures clean, gate exit 0).
- Forward uc-gate run before the next plan's design exists (the valid quadrant mode).
- pampa captures when served; factory abstraction (config-defined gates) named as follow-up.
