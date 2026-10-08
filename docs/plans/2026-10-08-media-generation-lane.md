---
status: active
created: 2026-10-08
updated: 2026-10-08
slug: media-generation-lane
---

# Plan: Media generation lane (gen1) for dev-decisions

## Context

dev-decisions is the consumer of its sibling lanes: sys1 judges what the work says, sdm1 scores what the work measures, sem1 indexes what the work looks like. On 2026-10-08 a fourth lane was born next door: [gen1](~/Projects/gen1) (v0.1.0, 100 tests green) generates what the work needs heard and seen — three provider-abstracted verbs, `speak` (text → speech, walking the cascade qwen → stepfun → kokoro and naming the leg that answered), `transcribe` (audio → text, stepfun ASR), `imagine` (prompt → image, wan). Durations are measured from the returned bytes, never the provider's claim; telemetry rows are structurally redacted (sha256 + counts, never content) and carry the `request_id` where a billed-but-failed leg is reconcilable; refusals are first-class (a missing key names the variable and the file it looked in). Nothing consumes it yet — gen1's own plan names this repo as the integration that waits for an ASR quality question worth calibrating, and the verification gate is exactly that question. This plan makes dev-decisions the first consumer: renders get verified against their scripts, ASR accuracy joins the calibration loop, media usage becomes a measured table, and every media call lands one accountable row beside every other decision.

## Approach

Reuse the proven seams instead of inventing new ones: the sibling bootstrap (the third copy — `_bootstrap_gen1` beside `_bootstrap_sys1`/`_bootstrap_sdm1`/`_bootstrap_sem1`); the fail-open wrapper covenant (`tabular_classify`'s never-raises shape); the advisory-gate discipline (`docs-gate` never blocks, and the `--via-semantic` cannot-block source guard); the recorder shape (`record-runs`: parse, write, no model); `forecast_band` reuse from `budget-gate`; and the fixture-pinning precedent (gen1's own C0/C2 fixtures, copied here with pinned sha256s so the regression never depends on a sibling's working tree). gen1 is consumed contract-only — this plan changes nothing in `~/Projects/gen1`; the public surface (`gen1.speak/transcribe/imagine`, `gen1.probe/doctor`, `registry_names`, the error taxonomy) is the pinned contract, and result keys are additive-only by house rule.

**Decision-model check.** The gate's per-line decision — does this transcript say this script line — is a deterministic text comparison, not a model judgment: order-preserving token agreement over lowercased alphanumeric tokens, counting in code like plan-gate. No sys1 head fits (there is nothing to judge: the ground truth is the script) and none is needed. `record-asr` is arithmetic over the same comparator. The single model call in this lane is sdm1's forecast in `media-budget`, batch-only. Rendering (`speak`, `imagine`) is never judged — gen1's operating rule, carried here: media renders what the work needs; whether it's good stays with the owner and the gates.

**Standing hard rule (carried from the tabular and semantic lanes):** no gen1 call ever runs inside a pre-commit / pre-push / ZCode-gate synchronous path — transcribe, speak, and imagine are each network or subprocess. Hooks are untouched; the socket-guard and hook-source tests prove it.

**Wire facts this plan is built on (live-verified 2026-10-08, from gen1's TECHNICAL-DOCUMENTATION.md):** `SPEAK_ORDER = (qwen, stepfun, kokoro)` — only speak has a local floor; `TRANSCRIBE_ORDER = (stepfun,)`; `IMAGINE_ORDER = (wan,)`. StepFun ASR normalizes punctuation (the em-dash does not come back — gen1's own C2 regression compares alphanumeric tokens, lowercased), so every comparison here is token-level, never byte-exact. StepFun `/audio/speech` caps input at 1000 chars and refuses by name (`ProviderCapabilityError`) rather than truncating. Durations are WAV-header-exact / MP3-frame-walked from the returned bytes. The telemetry sink is `~/.config/gen1/telemetry.jsonl` (`GEN1_TELEMETRY_FILE` override, `gen1.telemetry.set_sink()` in tests) and carries `input_sha256`, counts, provider, model, `duration_seconds`, `request_id` — never content. Keys `DASHSCOPE_API_KEY` / `STEPFUN_API_KEY` come from the environment or `~/.config/gen1/env` (chmod 600) — recorded, never moved. Errors are `Gen1Error` with `ProviderUnavailableError.submitted` the money flag: the wrappers pass gen1's cascade semantics through untouched and never re-issue a billed request. The seam dialect (gen1's `hyperframes.py`) is the gate's primary input: request `{"lines": [{"id", "text"}]}` and meta `{"voices": [{"id", "path", "duration_s", "provider", "format", "voice", "request_id"}], "total_duration_s", ...}` with paths relative to the hyperframes project dir.

High-level steps:

1. **gen1 consumes nothing here:** no change to `~/Projects/gen1` — the pinned public API is the contract. The fixtures `nar-s1.mp3` (sha256 `167cf7b4f62e1699c1c83607f7e36fbf2d16c185ddfacafdb404b7968665af6b`) and `nar-s1.txt` (sha256 `883166ae341158000c9740f126f5220b16c3c58963f72e36460cc8f8625ec37f`) are copied into `scripts/fixtures/media/`.
2. **media.py core:** `scripts/devdec/media.py` — `gen1_ready()` (the `sem1_ready` shape), the fail-open wrappers `media_speak` / `media_transcribe` / `media_imagine` (the `tabular_classify` covenant: never raise, return `{ok, …, error}`), the deterministic comparator `compare_text` (difflib token agreement), and the `GEN1_RAW_TAG = "gen1_raw"`.
3. **dev-decisions wiring:** `_bootstrap_gen1()` in `judgment.py` (env `DEV_DECISIONS_GEN1_PATH`, `~/Projects/gen1/src`, sibling search), `doctor` gen1 block after the sem1 block, comment block in `config.example.toml` after the sem1 one, CLI verbs under a `# ── media generation lane (gen1) ──` marker, JSONL rows tagged `gen1_raw/<leg>` until calibrated.
4. **`media-gate`:** the verification gate over the seam dialect (`--request`/`--meta`/`--project`) and the single-file mode (`--script`/`--audio`); advisory — WARN on gaps, never BLOCK — and registered in `_GATE_OP_TARGET_FIELD`.
5. **The utility verbs:** `media-transcribe`, `media-speak`, `media-imagine` — one accountable log row each.
6. **`record-asr`:** fixture round-trip → per-provider accuracy rows in the feedback store + report (the gradeable exception, made real).
7. **`record-media-runs`:** gen1 telemetry JSONL → the tabular lane's tables dir (parse, no model).
8. **`media-budget`:** sdm1 forecast band over recorded media usage (batch-only, fail-open).
9. **Selftests:** a `TestGen1Lane` class with the socket guard, the hook-source guard, and the cannot-block guard.
10. **Docs:** README lane section + Providers row, SKILL.md parallel section, recap at completion.

## Use cases

- [x] A: As a project owner I want a rendered narration verified against its script, so a video ships knowing the audio says what the script says — transcribed through the ASR lane, compared in code, gaps named line by line.
- [x] B: As the calibration loop I want ASR accuracy recorded against pinned ground truth, so the media gate's verdicts earn floors like every other judge — the one gen1 surface that is gradeable.
- [x] C: As a project owner I want media usage (audio seconds per provider, per day) recorded and forecast, so a batch render's cost is known before it runs.
- [x] D: As an engineer I want to speak, transcribe, and imagine from the dev-decisions CLI, so every media call lands one accountable row beside every other decision (`request_id` reconciliation included).
- [x] E: As an operator I want gen1's key/wire health in `doctor`, so a missing key is a named refusal before a batch, not a mid-render surprise.
- [ ] F: As a kit workflow I want a `media` grant (`world.speak/transcribe/imagine`), so router loops can render — deferred to the kit; the trigger is a kit workflow that adopts media.

## UX routes

None — no `docs/ux/` route contracts exist in this repo and no rendered-app UI changes are in scope; the new rows and tables are consumed through the existing CLI surfaces (no route contract change).

## Acceptance criteria

Order is identity: C0, C1, … in checkbox order; evidence-gate, plan-reconcile, and surfaces key on this order.

- [ ] C0: `_bootstrap_gen1()` in `judgment.py` follows the sdm1/sem1 bootstrap shape exactly (env `DEV_DECISIONS_GEN1_PATH` → `~/Projects/gen1/src` → sibling search over `parents[:4]`), resolves this machine's checkout with no pip install, and on a miss returns `None` with `sys.path` unchanged — a unit test plants a fake package for the hit and asserts the clean miss.
- [ ] C1: `gen1_ready()` mirrors `sem1_ready()` (importable + non-empty `registry_names()`), and `dev-decisions doctor` prints the gen1 block after the sem1 block with version plus per-provider key presence and reachability from gen1's own probe — presence only, never values; a test asserts the doctor source carries the block and that no key value can reach its output.
- [ ] C2: the three wrappers in `media.py` hold the `tabular_classify` covenant — they never raise, return `{ok, transcript/audio/images, result, error}`, name every refusal (the missing variable and the file it looked in, or the provider's own `Gen1Error` message), and tag every row `gen1_raw/<leg>`; stubbed-handle unit tests prove the `ok=False` paths for not-importable, missing-key, and provider-refusal.
- [ ] C3: `dev-decisions media-gate` verifies a seam render end to end — given the seam's request + meta (paths resolved against `--project`, a path escaping the project dir refused by name) or a single `--script`/`--audio` pair it transcribes each audio file, compares to the script line with the deterministic comparator, prints per-line agreement plus missing/extra tokens, logs one redacted row (agreement, token counts, input sha256s — never transcript content), and exits WARN on any gap; a stubbed-transcript unit test proves the matching pass and the mismatched gap, a refusal is reported by name (every line refused → exit 3, never 2), and the cannot-block source guard proves `EXIT_BLOCK` is unreachable.
- [ ] C4: `dev-decisions record-asr` transcribes the pinned in-repo fixture (`scripts/fixtures/media/nar-s1.mp3`, sha asserted in the test) and grades it against the pinned ground truth with C3's comparator — the same function, not a copy — writes a feedback-store row keyed `(task "asr_roundtrip", head "asr_faithful", provider "gen1_raw/<leg>")` and prints the per-provider accuracy report; a stubbed-transcript unit test proves the row shape and the join key the calibration command already reads.
- [ ] C5: `dev-decisions record-media-runs` parses gen1's telemetry JSONL (default sink, `GEN1_TELEMETRY_FILE` override, `set_sink()` in tests) into the tabular lane's tables dir with no model call — one row per attempt including failed legs (provider, model, `duration_seconds`, `request_id`) — and a corrupt line is skipped by name, never fatal; the parser is fixture-tested.
- [ ] C6: `dev-decisions media-budget` forecasts daily audio-seconds bands per provider from the recorded tables through the existing `forecast_band` (task `media_budget_forecast`), compares them against a requested budget, and degrades to the recorded table with a named reason when sdm1 is unavailable — never a crash, never a fabricated band; a stubbed-sdm1 unit test proves both paths.
- [ ] C7: a `TestGen1Lane` selftest class in the `TestSemanticLane` shape (name-merge into the under-test namespace, log/tables dirs isolated to a tempdir) carries the socket guard (no gen1 call reachable from `cmd_scan_staged`/`cmd_classify_diff`/`cmd_zcode_gate`), the hook-source guard (those functions' source never mentions gen1/media), and the hook files are byte-identical to before this work.
- [ ] C8: `config.example.toml` gains the lane's comment block after the sem1 one (discovery env, key homes recorded not moved, batch-only rule, `gen1_raw` tag), every rendering or scored verb's help string carries EVAL-ONLY until calibrated, and a test pins both the block and the tag.
- [ ] C9: README gains the Providers row and the `## Media generation lane (gen1)` section (the composition sentence extended, the operating rule — media renders, the gates judge — the batch-only rule, the command table), SKILL.md the parallel section, and the plan closes with per-criterion evidence plus a recap in `docs/recaps/`.

## Files to be touched

**gen1 (`~/Projects/gen1`):**
- No changes — consumed contract-only. `tests/fixtures/nar-s1.{mp3,txt}` are copied out with their pinned sha256s.

**dev-decisions (`~/Projects/dev-decisions`):**
- `scripts/devdec/judgment.py` — `_bootstrap_gen1()` sibling bootstrap (after the sem1 one, ~line 155)
- `scripts/devdec/media.py` — new module: readiness probe, the three fail-open wrappers, `compare_text`, `media-gate`, `media-transcribe`, `media-speak`, `media-imagine`, `record-asr`, `record-media-runs`, `media-budget`, `C<n>` section markers per criterion
- `scripts/devdec/workflow.py` — doctor gen1 block (after the sem1 block, line 994)
- `scripts/devdec/cli.py` — module import, seven verbs under the lane marker (after the semantic section)
- `scripts/devdec/gates.py` — `media-gate` registered in `_GATE_OP_TARGET_FIELD` (line 1686), target field
- `config.example.toml` — lane comment block after line 65
- `scripts/selftest.py` — `TestGen1Lane` (the `TestSemanticLane` setUp shape, socket guard, hook-source guard, cannot-block guard)
- `scripts/fixtures/media/nar-s1.mp3`, `scripts/fixtures/media/nar-s1.txt` — pinned copies (sha256 asserted in tests)
- `README.md`, `SKILL.md` — Providers row, lane section, EVAL-ONLY tags, batch-only rule
- `docs/recaps/` — session recap at completion

## Out of scope

- Hook-path media calls (the batch-only rule: transcribe/speak/imagine are each network or subprocess; revisit only with a measured local floor and a passed calibration floor).
- Rendering inside the gate (`media-gate` verifies the audio it is given; `speak` is a separate verb — generation is not idempotent, and a gate that re-rendered would double-bill).
- ASR calibration floors (`record-asr` accumulates the graded rows first; per-surface floors in the calibration command are the follow-up — thresholds don't transfer, the `modernbert_raw`/sem1 precedent).
- A cost/rate-card field on any row (gen1 v1 carries none; duration × provider is the measurable proxy — when a consumer must budget per call, sdm1's `billing_model_version` discipline is the template).
- Local ASR/image floors behind the wrappers (gen1's `transcribe`/`imagine` are single-provider in v1 and refuse by name when their wire is down).
- The kit `media` grant (`world.speak/transcribe/imagine` on the grant template) — trigger: a kit workflow that adopts media.
- Any quality scoring of generated media (the operating rule: media renders, the owner and the gates judge; nothing in `speak`/`imagine` feeds a verdict, a join, or a score).
- Voice cloning, music generation, video generation (gen1 out-of-scope rows; each becomes a registry provider when a consumer needs one).

## Verification

- C0–C2/C4–C8: `python3 scripts/selftest.py` — all green, the count grows by the `TestGen1Lane` cases; `python3 scripts/dev_decisions.py doctor` prints the gen1 row on this machine (checkout at `~/Projects/gen1/src`).
- C3 offline: stubbed-transcript fixtures in the selftest (matching line → pass with agreement 1.0; punctuation-only difference → pass at token level; dropped word → gaps with the token named; every line refused → exit 3).
- C3 live (keys in the env or `~/.config/gen1/env`): `python3 scripts/dev_decisions.py media-gate --script scripts/fixtures/media/nar-s1.txt --audio scripts/fixtures/media/nar-s1.mp3` — expected agreement 1.0 on the pinned fixture (the wire round-trip is word-exact per gen1 C2); skips cleanly without keys.
- C5: fixture telemetry JSONL with one good row and one corrupt line; the table lands the good row and names the skipped one.
- C7: socket-guard test patches `socket.socket` to raise during a hook-path invocation; `grep -rn "gen1" scripts/devdec/workflow.py scripts/devdec/gitops.py` returns nothing inside hook functions; `grep -c "EXIT_BLOCK" scripts/devdec/media.py` → 0.
- C9: README/SKILL greps for the lane, the `gen1_raw` tag, and the batch-only rule; recap in `docs/recaps/`.

## Linked artifacts

- `~/Projects/gen1/TECHNICAL-DOCUMENTATION.md` — §2 providers, §5 telemetry, §7 limits (the wire facts this plan consumes)
- `~/Projects/gen1/src/gen1/hyperframes.py` — the seam dialect the gate reads (request/meta shapes, project-relative paths)
- `~/Projects/gen1/tests/fixtures/nar-s1.{mp3,txt}` — copied to `scripts/fixtures/media/` with pinned sha256s (the C4 round-trip regression)
- `~/Projects/dev-decisions/README.md` + `SKILL.md` — media lane section: commands, `gen1_raw` filter, batch-only rule
- `~/Projects/dev-decisions/config.example.toml` — the lane's comment block (discovery env, key homes, batch-only)
- `docs/recaps/` — session recap at completion

## Risks

- The wire round-trip is word-exact, not byte-exact (StepFun normalizes punctuation) — the token comparator is the mitigation, and it is gen1's own regression rule, not an invention.
- Long narration lines past StepFun's 1000-char cap refuse by name mid-batch — the gate reports the refusal and the short line, never truncates; the seam renders line-wise, so the cap is a per-line concern the composition owns.
- `media-gate`'s verdict rides an uncalibrated ASR — hence advisory-only (WARN, never BLOCK), `gen1_raw`-tagged, until `record-asr` accumulates the rows that earn floors.
- The seam's meta paths are project-relative — the gate resolves them against `--project` and refuses a path that escapes the project dir rather than reading outside it silently.
- gen1 is one day old — the pinned public contract is the stability bet; result keys are additive-only by house rule, so the wrappers read only the pinned keys and never a positional detail.

## Dependencies

- gen1 (`~/Projects/gen1`, v0.1.0) — the consumed lane (imported, stable; born-verified 2026-10-08).
- sys1 (`~/Projects/sys1`) — bootstrap precedent (imported, stable).
- sdm1 (`~/Projects/sdm1`) — fail-open wrapper and `forecast_band` precedents; `media-budget` reaches it through `tabular.py` (pattern reuse, batch-only).
- sem1 (`~/Projects/sem1`) — readiness-probe and eval-only-tag precedents (pattern reuse, no code dependency).
- dev-decisions `main` — the consumer repo (tabular lane at `d8de96a`, semantic lane at `d688e27`).
