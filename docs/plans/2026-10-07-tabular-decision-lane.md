---
status: active
created: 2026-10-07
updated: 2026-10-07
slug: tabular-decision-lane
---

# Plan: Tabular decision lane (sdm1 / TabPFN hosted) for dev-decisions

## Context

dev-decisions' active provider roster (glide, drex, jev, decide, GLiNER) is entirely text-judgment: diffs, plans, logs, docs. The sdm1 library (v0.1.0, `~/Projects/sdm1`) extends the same provider-abstraction pattern to tabular foundation models, and the Prior Labs TabPFN hosted API is now verified live from this machine (stdlib urllib only, key wired in sdm1's gitignored `CLAUDE.local.md`, end-to-end classification smoke passed 2026-10-07 on model v3.5). That unlocks six dev-workflow use cases where the evidence is a table rather than prose: CI run histories, benchmark series, the dev-decisions JSONL/feedback calibration stores, git-history features, fleet metric tables, and structured issue features. This plan adds the tabular lane to sdm1 (hosted provider + task-type extensions), wires it into dev-decisions' existing task-registry / gate-family / calibration machinery, and updates both repos' documentation and archify diagrams.

## Approach

Reuse the two proven seams instead of inventing new ones. On the sdm1 side, the provider registry + subprocess-free HTTP provider slot is exactly what v0.1.0 scoped ("registry makes non-SDM backends cheap"), and the verified smoke flow (`prepare_train_set_upload` → signed PUT → `fit` → `prepare_test_set_upload` → `predict`) becomes `providers/tabpfn_hosted.py` with `TABPFN_API_KEY` from the environment. On the dev-decisions side, the sys1 bootstrap pattern (`_bootstrap_sys1`) gains an sdm1 sibling, `by_task` routing overrides gain an sdm1 target, and the six use cases become new batch tasks and gate-family members that read CSV tables under `~/.local/share/dev-decisions/tables/`.

**Decision-model check.** Every use case here is itself a decision-model fit, by construction: per-row gated classifications (flake, override probability, commit risk, component route), a quantile-band gate (budget), and zero-shot anomaly flags (fleet watch). The lane they run on is sdm1/TabPFN (calibrated per-row probabilities over structured rows); sys1 stays authoritative for prose judgment; the composition rule is "sys1 reads what the work says, sdm1 scores what the work measures," and both land in the same JSONL + feedback + earned-autonomy loop. The standing hard rule carries over: measured small-task latency is ~162s, so **no TabPFN network call ever happens inside pre-commit / pre-push / ZCode-gate synchronous paths** — hooks read locally cached verdict tables produced by batch commands.

**Contract evolution (declared, user-owned).** This plan deliberately extends two things sdm1's TECHNICAL-DOCUMENTATION currently freezes: (1) the §4 `task_type` enum `classification | regression` gains `forecast` and `anomaly` (additive — result keys are only ever added, and §2's own `<add-when-implemented>` placeholder anticipates task-type growth); and (2) the §2 Backends row, currently documented as the `[models]` venv reached via subprocess, gains a second documented backend pattern: hosted HTTP via stdlib `urllib` (no torch, no venv, key from env). Both re-normings are exactly what C9's contract-doc updates ship; until then the arch-gate rubric is intentionally behind this plan.

**Docs and diagrams are part of the work, not an afterthought** (standing rule): sdm1 README / TECHNICAL-DOCUMENTATION / FUNCTIONAL-SPECIFICATIONS / architecture overview and dev-decisions README / SKILL.md / config.example.toml / archify diagrams are in-scope artifacts with their own acceptance criterion (C9), and the recap will sweep them.

High-level steps:

1. **sdm1 Phase 1 — `tabpfn-hosted` provider (classification):** `providers/tabpfn_hosted.py` from the smoke flow (stdlib `urllib`, Bearer `TABPFN_API_KEY`), registry registration, availability probe via `GET /tabpfn/get_model_limits`, parsers for the predict response, `tabpfn` confidence-floor class in `interpret.py` (per-model-class semantics rule), telemetry keys (`data_tokens`/`prediction_tokens` when returned, `billing_model_version`, `execution_mode`, `cache_outcome`), 429/quota → `ProviderUnavailableError` with reset info, doctor/models CLI rows. Seed: `/tmp/sdm1_tabpfn_smoke.py` (rewrite properly, tests use stubbed HTTP).
2. **sdm1 Phase 2 — task-type extensions:** extend `TableTask` with `forecast` (point + quantiles, TabPFN-TS surface) and `anomaly` task types behind the same `TableResult` contract (adding keys is legal; renaming is not), parsers + interpret semantics per type, `[models]`-free hosted path only.
3. **dev-decisions wiring:** `_bootstrap_sdm1()` mirroring the sys1 bootstrap (env `DEV_DECISIONS_SDM1_PATH`, `~/Projects/sdm1/src`, sibling search), `doctor` sdm1 row, `by_task` override target `sdm1` documented in `config.example.toml`, JSONL rows for tabular tasks (provider `sdm1`, task, heads, latency, telemetry passthrough).
4. **Use case — gate the gate (C2):** `dev-decisions override-prior` batch command reads the JSONL event log + `logs/feedback/feedback.jsonl`, builds a per-head feature table (task, head, provider, confidence bin, hour-of-day, verdict), scores override probability via sdm1, prints per-head override priors + suggested floors next to the current static floors; output feeds the calibration report and the tower earned-autonomy board.
5. **Use case — run-history gate (C3+C4):** `dev-decisions record-runs [--repos ...]` ingests CI run history via `gh` (workflow runs + jobs, per test-suite mapping where available) into idempotent CSV tables; `dev-decisions history-gate` scores per-test flake probability from those tables (batch) and emits gate rows compatible with `disposition` and the tower gates.html ingestion.
6. **Use case — budget gate (C5):** `dev-decisions record-bench <cmd...>` appends a timing row (ts, name, duration, commit) to a benchmark table; `dev-decisions budget-gate <table>` produces an sdm1 `forecast` quantile band per metric and flags out-of-band latest rows; advisory gate rows.
7. **Use case — commit-risk prior (C6):** `dev-decisions risk-prior` scores per-directory features from local git history (churn, size, file types, past revert touches) into a cached risk table; the pre-push path reads the cached table only (no network) and composes the prior into the advisory verdict beside classify-diff.
8. **Use case — fleet anomaly watch (C7):** `dev-decisions fleet-anomaly` builds repo metric tables (from existing fleet-scan/status data + recorded tables) and scores them zero-shot via sdm1 `anomaly`; flags land as gate rows (tower inbox).
9. **Use case — component routing (C8):** extend the `issue_triage` task with an sdm1 many-class routing head over structured issue features; **eval-only** until the calibration floor is met (tagged like `modernbert_raw` so production metrics can filter it).
10. **Docs + diagrams (C9):** update both repos' README/SKILL/config and sdm1 contract docs; update archify sources (`docs/architecture/system.drawio`) and rebuild `system.svg` in BOTH repos (committing sdm1's currently untracked diagrams); reconcile the three legacy hand-authored SVGs still embedded in dev-decisions README (`architecture.svg`, `workflows.svg`, `decision-flow.svg`) with the new lane; XML-validate and visual-judge rendered PNGs.

## Use cases

- [ ] A: As a maintainer I want per-test flake probabilities computed from CI run history so that pre-push advisories can name likely-flaky tests instead of only judging diff text.
- [ ] B: As a maintainer I want benchmark metrics scored against a forecast band so that a change that regresses latency or size is flagged even when no baseline file is maintained by hand.
- [ ] C: As the owner of the calibration loop I want override probabilities and fitted floors predicted from the accumulated JSONL + feedback tables so the earned-autonomy board carries a learned prior instead of raw 20-case counts alone.
- [ ] D: As a committer I want a per-directory risk prior from git history composed into the pre-push advisory so numeric history evidence accompanies the text verdict, without adding any network call to the hook.
- [ ] E: As a fleet owner I want zero-shot anomaly flags over repo metric tables surfaced in the tower inbox so metric regressions appear without hand-maintained thresholds.
- [ ] F: As a triage user I want issues routed to components from structured features as an eval-only head so routing quality can be measured against accruing labels before it is trusted.

## UX routes

None — no `docs/ux/` route contracts exist in this repo and no rendered-app UI changes are in scope; tower `gates.html` surfaces the new gate rows through the existing gate-event ingestion (no route contract change).

## Acceptance criteria

Order is identity: C0, C1, … in checkbox order; evidence-gate, plan-reconcile, and surfaces key on this order.

- [ ] C0: `sdm1.classify()` with the `tabpfn-hosted` provider completes an in-memory classification task end-to-end using only stdlib imports, raises `ProviderUnavailableError` naming `TABPFN_API_KEY` when unset, and its telemetry records carry `billing_model_version`, `execution_mode`, and `cache_outcome` keys with no dataset cell values and no key values (redaction test proves both).
- [ ] C1: dev-decisions discovers sdm1 through a bootstrap sibling to the sys1 one, `dev-decisions doctor` prints an sdm1 availability row, a `by_task` override of `safety = "sdm1"` routes that task to sdm1 in a `--dry-run` invocation, and the resulting JSONL row carries provider `sdm1` with the sdm1 telemetry dict.
- [ ] C2: `dev-decisions override-prior` scores the local JSONL + feedback stores via sdm1 and prints per-head override probabilities plus suggested floors, and a fixture test with a synthetic log ranks a planted 100%-overridden head first.
- [ ] C3: `dev-decisions record-runs` writes per-repo CI history CSV tables under `~/.local/share/dev-decisions/tables/` from `gh`, a second identical run adds zero duplicate rows, and the `gh` output parser is unit-tested against a captured fixture file.
- [ ] C4: `dev-decisions history-gate` scores recorded tables and exits 1 with a flag for a fixture table containing a planted intermittent-failure test while a stable-fixture run exits 0, and both runs append gate rows carrying `input_sha256` that `dev-decisions disposition history-gate <target>` can find and pair.
- [ ] C5: `dev-decisions record-bench` appends a timing row per invocation and `dev-decisions budget-gate` flags a fixture series whose latest value falls outside the sdm1 forecast band while passing an in-band fixture, using the sdm1 `forecast` task type added in this work.
- [ ] C6: `dev-decisions risk-prior` writes a per-directory risk table from git-history features, and the pre-push hook path consumes only that cached table — a test asserting zero outbound network calls during `classify-diff --with-risk-prior` passes.
- [ ] C7: `dev-decisions fleet-anomaly` scores a metric table via the sdm1 `anomaly` task type and appends gate rows for flagged repos, and a fixture table with a planted outlier produces exactly that repo's flag.
- [ ] C8: `triage-issues --provider auto` runs the sdm1 component-routing head in eval-only mode: its predictions appear in the JSONL under a filterable tag, no labels are applied to issues from it, and a unit test proves the label-application path is not reachable from the sdm1 head.
- [ ] C9: Both repos' README state the tabular lane and its batch-only rule, SKILL.md documents the new commands, `config.example.toml` carries the sdm1 provider block and by_task examples, sdm1's TECHNICAL-DOCUMENTATION (§4–§7), FUNCTIONAL-SPECIFICATIONS (§2–§3, §7), and architecture overview match the shipped provider, and the archify diagrams in both repos (`docs/architecture/system.drawio` → rebuilt `system.svg`, including committing sdm1's untracked diagrams) render the new lane, pass XML validation, and pass a visual-judge review of rendered PNGs.

## Files to be touched

**sdm1 (`~/Projects/sdm1`):**
- `src/sdm1/providers/tabpfn_hosted.py` — new hosted provider (REST flow, auth, 429 semantics)
- `src/sdm1/types.py` — `forecast`/`anomaly` task types + result keys (additive)
- `src/sdm1/parsers.py`, `src/sdm1/interpret.py` — per-type normalization + `tabpfn` floor class
- `src/sdm1/telemetry.py` — token/billing keys
- `src/sdm1/cli.py` — doctor/models rows for the new provider
- `tests/test_providers_tabpfn_hosted.py`, `tests/test_parsers.py`, `tests/test_interpret.py`, `tests/test_telemetry.py` — provider/parsing/floor/redaction tests with stubbed HTTP
- `pyproject.toml` — no runtime deps change; `[models]` extra untouched
- `docs/architecture/system.drawio` + `system.svg` — archify update + rebuild + first commit of these files
- `README.md`, `TECHNICAL-DOCUMENTATION.md`, `FUNCTIONAL-SPECIFICATIONS.md`, `docs/architecture/overview.md`, `docs/STATE-SNAPSHOT.md` — lane documentation

**dev-decisions (`~/Projects/dev-decisions`):**
- `scripts/devdec/config.py`, `scripts/devdec/judgment.py` — sdm1 bootstrap, provider registration, by_task target
- `scripts/devdec/workflow.py` — `record-runs`, `risk-prior` cache-read integration in the pre-push path
- `scripts/devdec/gates.py` — `history-gate`, `budget-gate`, `fleet-anomaly`, `override-prior`, disposition pairing for the new gates
- `scripts/devdec/cli.py` — subcommand dispatch
- `scripts/devdec/dashboard.py` — sdm1 rows surface via existing endpoints (telemetry shape passthrough)
- `config.example.toml` — sdm1 provider block, by_task examples, batch-cadence notes
- `tests/` (or `scripts/selftest.py` cases) — fixture tests per criterion
- `README.md`, `SKILL.md`, `docs/architecture/system.drawio` + `system.svg`, `docs/architecture.svg`, `docs/workflows.svg`, `docs/decision-flow.svg` — docs + diagrams
- New: `docs/plans/` (this file is its first resident)

## Out of scope

- Inline (synchronous hook-path) TabPFN calls — the ~162s small-task latency rule makes this permanent, not a TODO.
- The three pre-registered business tasks (`overdue_flag`, `no_show`, `outcome_likelihood`) in commissiontracker / pampa-wineclub / jobhound — separate later plan; this plan ships only the lane they will run on.
- TabPFN fine-tuning, RelArena-α multi-table relational tasks, embeddings, and data-generation surfaces — sdm1 routes and normalizes, never trains; relational stays a later plan.
- Absorbing sdm1 into sys1's registry — sdm1 stays a sibling library with its own contract docs; dev-decisions bootstraps it the way it bootstraps sys1.
- Creating a TECHNICAL-DOCUMENTATION.md for dev-decisions — a known conventions gap (arch-gate has no rubric there); flagged for its own future plan.
- Tower-side UI work beyond what existing gate-event ingestion already renders; no new tower pages.

## Verification

**C0 (sdm1 provider):**
```bash
cd ~/Projects/sdm1 && .venv/bin/python -m pytest -q tests/test_providers_tabpfn_hosted.py && .venv/bin/ruff check .
TABPFN_API_KEY= .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import sdm1; sdm1.classify(rows=..., provider='tabpfn-hosted')"  # expect ProviderUnavailableError naming TABPFN_API_KEY
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import sdm1; r=sdm1.classify_csv(...); print(r.telemetry)"  # live: billing_model_version == 'v3.5'
```
Expected: suite green, unavailable path actionable, live telemetry keys present, redaction test green.

**C1 (dev-decisions wiring):**
```bash
dev-decisions doctor | grep sdm1
dev-decisions classify-diff --diff-file <small.diff> --dry-run   # with by_task safety=sdm1 in test config
```
Expected: doctor row present; JSONL row shows provider `sdm1`.

**C2 (override-prior):** run against a synthetic-log fixture (planted head with 5/5 overridden) — expected: planted head ranked first; command exits 0 on the real store even while floors are still "gathering".

**C3 (record-runs):** `dev-decisions record-runs --repos dev-decisions` twice; `sort -u` row count identical between runs; parser unit test on a captured `gh` fixture.

**C4 (history-gate):** fixture table with intermittent failure → exit 1 + flag; stable fixture → exit 0; then `dev-decisions disposition history-gate <target> --status waived --reason test` finds the gate row.

**C5 (budget-gate):** `record-bench` appends a row (file line-count check); out-of-band fixture → exit 1 with band printed; in-band fixture → exit 0.

**C6 (risk-prior):** table exists with per-directory rows; `classify-diff --with-risk-prior` under a network-blocking guard (unit test monkeypatching socket) completes — proving the cached-table-only rule.

**C7 (fleet-anomaly):** fixture table with one planted outlier → exactly one gate row naming that repo.

**C8 (component-route):** unit test asserts the sdm1 head's predictions never reach `gh pr edit`/`gh issue edit` label paths; JSONL rows carry the eval-only tag.

**C9 (docs + diagrams):** README grep for the lane + batch-only rule in both repos; `python3 -c "import xml.dom.minidom,sys; xml.dom.minidom.parse('docs/architecture/system.drawio')"` in both repos; render `system.svg` to PNG and run the visual-judge pass; sdm1 doc sections match the shipped wire (spot-check table of result keys).

## Linked artifacts

**sdm1:**
- `TECHNICAL-DOCUMENTATION.md` §2 "Tech Stack" — Backends row gains the hosted-HTTP pattern (urllib, key from env, no venv)
- `TECHNICAL-DOCUMENTATION.md` §4–§7 — provider wire, `forecast`/`anomaly` `task_type` enum growth, result keys, telemetry keys, CLI
- `FUNCTIONAL-SPECIFICATIONS.md` §2–§3, §7 — new provider behavior, edge cases (quota, reset, signed-URL expiry)
- `docs/architecture/overview.md` — module map + data flow rows for the hosted path
- `docs/architecture/system.drawio` / `system.svg` — archify update, rebuild, first commit
- `README.md` — status line (scaffold → hosted provider live), provider table, batch-latency note

**dev-decisions:**
- `README.md` — new commands (override-prior, record-runs, history-gate, record-bench, budget-gate, risk-prior, fleet-anomaly), provider table row, gate-family table, batch-only rule
- `SKILL.md` — per-command reference for the six new surfaces
- `config.example.toml` — sdm1 block + by_task examples
- `docs/architecture/system.drawio` / `system.svg` — archify update (tabular lane in the data flow), rebuild
- `docs/architecture.svg`, `docs/workflows.svg`, `docs/decision-flow.svg` — reconcile embedded legacy diagrams with the new lane
- `docs/recaps/` — session recap per the house cycle

## Risks

- **Latency/cost of full-history scoring** — run-history and override-prior tables can grow; mitigate with row caps per request, token-cost estimate before each call (v2.x formula: `max(5000, rows×cols×n_estimators)`), and the 50M/day pool as the tripwire.
- **Hosted dependency** — gates must degrade offline: cached verdict tables keep gates advisory-functional with zero network; 429 → actionable unavailable row, never a crash (fail-open rule).
- **Thin labels for component routing** — eval-only until a floor is met, mirroring the ModernBERT policy; no user-visible behavior change until then.
- **Doc drift across six surfaces in two repos** — C9 exists precisely for this; the standing "gate feature commits touch README + SKILL.md together" lesson applies to every new command commit.
- **sdm1 contract discipline** — `forecast`/`anomaly` extensions are additive keys only; the 19-key result contract is frozen by `tests/test_types.py` and must stay green unmodified.

## Dependencies

- TabPFN hosted account + `TABPFN_API_KEY` (wired and live-verified 2026-10-07 in sdm1's `CLAUDE.local.md`)
- sdm1 v0.1.0 (`5ab37bc`, pushed) — this plan's Phase 1 is sdm1's second plan after `2026-09-29-sdm1-foundation.md`
- dev-decisions main (gate family, calibration loop, disposition, tower ingestion as merged 2026-10-02/04)
- sys1 (bootstrap pattern reference; runtime dependency of dev-decisions' text lane, unchanged here)

## Notes

- Plan location convention: dev-decisions had no `docs/plans/` (prior work was planned in sibling repos); this file creates it. Flagged consciously rather than silently.
- The smoke flow, metering formula, tier limits (v3.5: 160 classes, 1M rows), and the measured 162s latency all come from the 2026-10-07 live verification recorded in the session memory; treat exact quota numbers as needing re-check at build time.
- Alternatives considered: routing tabular tasks through sys1's registry (rejected — sdm1 is the tabular sibling with its own contract; double abstraction buys nothing); calling TabPFN from the pre-push hook directly (rejected — latency + the batch-only rule); starting with the run-history gate instead of gate-the-gate (rejected — override-prior needs no new data collection and strengthens the floor machinery every other gate depends on, so it lands first).
- Legacy-diagram decision deferred to build time with evidence: if the three embedded SVGs duplicate the archify content, supersede them (README embeds `system.svg` renders) instead of hand-editing six files; README references must resolve either way (C9).
