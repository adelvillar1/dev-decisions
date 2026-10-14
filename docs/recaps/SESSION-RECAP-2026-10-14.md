# Session Recap — 2026-10-14

## Summary

Shipped the **blend1 synthesis lane** (plan
`docs/plans/2026-10-14-blend1-synthesis-lane.md`, completed): the fifth model
class under the accountability machinery — sys1 judges what the work SAYS,
sdm1 scores what it MEASURES, sem1 indexes what it LOOKS LIKE, gen1 renders
what it needs HEARD AND SEEN, and blend1 **COMPOSES**. The first loop,
**explain-the-gate**: every verdict-emitting gate gains a second beat — a
synthesized, validated explanation of what the verdict means and what the
author should change, riding in the JSONL beside the verdict, keyed by
`input_sha256`. The calibration dataset upgrades from scores to readable
causes.

## Plans worked on

- **`docs/plans/2026-10-14-blend1-synthesis-lane.md`** — status: **completed**.
  All 8 ACs met (7 offline + the live explain, verified on the real dataset).

## Commits

| Hash | Message |
|------|---------|
| `411d2a6` | feat: blend1 synthesis lane — explain-the-gate loop (eval-only, batch-only) |
| `5e5ad85` | fix: synthesis-lane review findings — untrusted-data framing for the artifact, budget-gate in the gate-op set, ambiguous --row prefix refused, one exit convention (any refusal non-zero), venv install hint, 5 regression tests |
| `c6b7da3` | test: install-hint assertion follows the venv/uv-sync path |

## What was added

- **`scripts/devdec/synthesis.py`** — the lane: `explain_gate_row` (artifact
  sha256 vs the row's `input_sha256` — prefix-or-full either direction, the
  house `_sha16` convention; a drifted artifact is refused by name),
  `blend1.ask(prompt)` on the default auto path (`live=True` never passed),
  the explanation written to `logs/feedback/explanations/<sha>.md` (text
  never enters the redacted JSONL — the recoverable-text pattern),
  `synthesis_budget()`, and the row/gate-op selectors (`--row`,
  `--last-failed`, `--date`, `--dry-run`). Never raises; every refusal named.
- **CLI**: `dev-decisions explain` + `dev-decisions synthesis-budget`.
- **Tests**: 19 new in `TestBlend1Lane` (selftest.py) — the exact row shape,
  the hash refusal, the fail-open install message, the AST proof that no
  verdict-applying path is reachable, the C8-pattern socket guard, budget
  aggregation, dry-run zero-ask. Suite: **110 passed** (91 pre-existing + 19).
- **Docs**: README provider row + lane section, SKILL.md commands,
  config.example.toml lane paragraph.

## Live verification (parent-run, real dataset)

`explain --artifact scripts/fixtures/media/nar-s1.txt --row 883166ae34115800`
(a real media-gate row): validation **passed** (atq 0.908, contra 0.259,
numbers ok), 1,105 chars, **$0.00177**, 11.1 s. The row landed in the real
log (`op: "explain"`, `providers_used: [gen1_raw/stepfun, blend1_raw]`, route
ng 0.818 → synthesized / nlf 0.037 → web correctly not fired) and
`synthesis-budget` reads it. Run under blend1's venv — a bare
`pip install -e ~/Projects/blend1` cannot resolve the sys1/sem1/sdm1 path
deps on this box.

## Review cycle

Spec review: SPEC-COMPLIANT. Quality review: REQUEST_CHANGES — four
important items, all fixed by the parent in `5e5ad85` before the live run:
untrusted-data framing for the artifact in the ask prompt (a diff carrying
prompt-shaped text must not steer the synthesizer), `budget-gate` added to
the gate-op set (the whitelist missed it), ambiguous short `--row` prefixes
refused by name (a 1-char prefix matches 1,059 distinct artifacts on the
real log), and one exit convention (any refusal → non-zero). Plus the
install hint corrected to the venv/uv-sync path (verified: the pip path
errors on this box).

## Open questions / next steps

1. **Explanation calibration floors** — `blend1_raw` stays eval-only until
   rows accumulate; fitting floors is the later, evidence-backed step (the
   sem1 lane's own arc).
2. **The other aggregated shapes** (scoped out of this plan, in priority
   order): web-verified gates (the web lane × docs/evidence gates — claims
   about the external world judged against live primary sources);
   dashboard-askable (blend1 × the dashboard query loop); per-project
   corpora + warmup freshness pointed at dev-decisions' own decision
   history (the blend1 feature exists; the wiring is separate).
3. **Selector coverage** — `--date` batches re-read the artifact per row
   (the hash gate makes a mid-batch change a named refusal); a quiet/JSON
   output mode for large batches is unbuilt.
