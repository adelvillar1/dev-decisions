# Session recap — 2026-10-07 — tabular decision lane

**Plan link**: `docs/plans/2026-10-07-tabular-decision-lane.md` (active; C0-C9 implemented on `feature/tabular-lane`)

## What shipped

- **Seven batch surfaces** on the sdm1 library's new `tabpfn-hosted` backend (Prior Labs TabPFN-3.5 REST): `override-prior` (per-head override probabilities + suggested floors from the events x feedback join by `input_sha256`), `record-runs` (gh CI history → `tables/ci_runs.csv`, idempotent, `--jobs` for check-level rows), `history-gate` (intermittent-check flags; mechanical cold start, sdm1-ranked at >= 3 graded rows), `record-bench`/`budget-gate` (forecast band, out-of-band flags), `risk-prior` (per-directory revert prior; P(yes) complement for single-class responses), `fleet-anomaly` (coverage-based repo flags; mechanical fallback).
- **C1 wiring**: `_bootstrap_sdm1` mirrors the sys1 bootstrap; `doctor` prints sdm1 provider health; `[classify] by_task` routes a task to `sdm1` — the bridge DECLINES without labeled tabular context (declines are first-class) and the declined run now logs a JSONL row.
- **C6 batch-only rule**: `classify-diff --with-risk-prior` reads the cached table only — socket-guarded test proves zero network in the hook path.
- **C8 eval-only head**: `triage-issues --sdm1-route` scores component routing from structured, text-free features — logged, never applied.
- **selftest 41 green** (tabular lane fixtures: gh parsers, candidate mechanics, override ranking with planted head, forecast guard, numstat parsing, socket-guarded table reads).

## Live verification (real API, 2026-10-07)

- C1: by_task `safety = "sdm1"` → JSONL row provider=sdm1, telemetry carries the decline reason.
- C2: `override-prior` — 68 heads graded through the join, scored by hosted TabPFN.
- C5: four `record-bench` rows; `budget-gate` forecast band [0.119, 0.125] vs actual 0.122 → ok.
- C6: `risk-prior` over 11 directories (hosted; empty priors before the fix were a single-class response — complement now derived).
- C7: `fleet-anomaly` — 24 repos scored, 0 flagged, 24 in band.
- C3/C4: ingest path verified live against `adelvillar1/dev-decisions` (repo has no Actions → honest empty table; parser + mechanics pinned by unit tests).

## Diagrams (C9)

Legacy hand-authored SVGs (`architecture.svg`, `decision-flow.svg`, `workflows.svg`) and the drawio pair REPLACED with archify: `docs/architecture/system.{candidate.json,html,png}` (candidate = source of truth per the archify migration). finalize gates all pass (validate/deliver/check/browser-check); documents:visual-judge PASS on light+dark 2048px captures. README embeds the new PNG and links the interactive HTML.

## Deferred

- `history-gate` sdm1 ranking waits for >= 3 graded dispositions on its flags (cold start is mechanical by design).
- Component routing stays eval-only until a calibration floor is met.
- The monolith fallback (`scripts/dev_decisions.py`) does not carry the tabular lane — package-only per the split convention.
- Merge to main awaits explicit user approval.
