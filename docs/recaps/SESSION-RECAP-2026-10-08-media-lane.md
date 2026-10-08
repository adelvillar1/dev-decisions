# Session recap — media generation lane (gen1) in dev-decisions, 2026-10-08

Plan: `docs/plans/2026-10-08-media-generation-lane.md` (committed on main at `146d75c` before any code, closed this session with per-criterion evidence). Library: [adelvillar1/gen1](https://github.com/adelvillar1/gen1) (v0.1.0) — consumed contract-only; nothing changed next door.

## What shipped

**dev-decisions** (branch `feature/media-generation-lane`): `_bootstrap_gen1` beside the three sibling bootstraps; `scripts/devdec/media.py` with `gen1_ready()`, the three fail-open wrappers (`media_speak`/`media_transcribe`/`media_imagine`, every row tagged `gen1_raw/<leg>` until calibrated), the deterministic token comparator `compare_text`, and seven CLI verbs:

- `media-gate` — verifies rendered narration against its script (seam request + meta resolved against `--project`, escaping paths refused by name, or a single `--script`/`--audio` pair); per-line agreement with missing/extra tokens printed, one redacted log row, WARN on any gap, never blocks; registered in `_GATE_OP_TARGET_FIELD`.
- `media-transcribe` / `media-speak` / `media-imagine` — the utility verbs, one accountable row each.
- `record-asr` — pinned fixture round-trip graded with the gate's own comparator into the feedback store under `asr_roundtrip` / `asr_faithful` / `gen1_raw/<leg>`, the row `calibration` already joins.
- `record-media-runs` — gen1's telemetry JSONL into `tables/media_runs.csv` with no model call, occurrence-keyed so byte-identical attempts stay distinct and re-runs land nothing twice.
- `media-budget` — sdm1 forecast band over recorded audio-seconds per provider vs a budget; sdm1 missing degrades to recorded stats with a named reason and no fabricated band.

Plus the house wiring: doctor block after sem1 (version + four providers, key presence by variable name only), `config.example.toml` block after sem1, EVAL-ONLY help on the five rendering/scored verbs, fixtures `scripts/fixtures/media/nar-s1.{mp3,txt}` with sha256s pinned in tests, README Providers row + `## Media generation lane (gen1)` section, SKILL.md parallel section, and `TestGen1Lane` — 18 tests, full suite **75 green** (57 before).

Additive side change recorded in the plan closeout: `tabular.py`'s `forecast_band` gained defaulted `task_id`/`table_name` kwargs so `media-budget` can name its task `media_budget_forecast`; existing callers unchanged.

## Criterion evidence (C0–C9)

- **C0** bootstrap hit (fake package planted via `DEV_DECISIONS_GEN1_PATH`, lands at `sys.path[0]`) and clean miss (`sys.path` byte-equal) both tested.
- **C1** `doctor` prints `gen1: v0.1.0 (gen1 + kokoro, qwen, stepfun, wan available; eval-only until calibrated)` + four presence-only provider rows; doctor source test rejects any key-value path.
- **C2** wrappers proven never-raising on not-importable, missing-key (names `STEPFUN_API_KEY` and the file it looked in), and provider refusal; `compare_text` passes em-dash/case noise at agreement 1.0 and names a dropped token.
- **C3** live on the pinned fixture: `agreement=1.000 (12/12)`, verdict pass, exit 0; tests cover gap→WARN, all-refused→exit 3, seam path-escape refusal, redacted log row; `grep -c EXIT_BLOCK scripts/devdec/media.py` → 0.
- **C4** live `record-asr`: accuracy 1.000, and `calibration --min-rows 1 --min-span 0` joins the row (`n=1 acc=1.0 … YES`); the test pins both fixture shas and asserts the comparator is called, not copied.
- **C5** live ingest of the real sink: 341 rows, re-run `+ 0 new row(s), 341 duplicate(s)`; corrupt line skipped by name in the fixture test.
- **C6** stubbed-sdm1 test proves band path (task id reached `forecast_band`, over-budget → WARN) and degraded path (named reason, recorded stats, no fabricated band); live degraded exits 0 within budget and 1 over.
- **C7** socket guard (clean command under a raising `socket.socket` → exit 0), hook-source guard (three hook functions mention neither gen1 nor media), hook files byte-identical to the pinned template.
- **C8** config block and EVAL-ONLY help pinned by test from argparse's registered help strings plus the config text; `GEN1_RAW_TAG` pinned as a constant.
- **C9** README/SKILL sections with the four-lane composition sentence, the media-renders-gates-judge rule, the batch-only rule, and the command table; plan closed with evidence; this recap.

## Lessons

- The monolith's `except Exception: pass` delegation bit again: a `read_table` unpack bug (it returns `(columns, rows)`) surfaced as the legacy parser's "invalid choice". When a package command dies mysteriously, run `devdec.cli.main` directly for the real traceback.
- Byte-identical rows in gen1's telemetry sink are legitimate attempts — failed legs carry null `request_id` and duration 0.0 — so content-sha-only dedupe collapsed 341 lines to 55. The ingest key is content sha + occurrence index, which keeps both semantics honest and re-runs idempotent.
- This machine has no `~/.config/gen1/env`; `STEPFUN_API_KEY` lives in `~/.hermes/.env` and was injected process-locally for the live runs (name only, never a value). gen1's resolution contract was left untouched.
- argparse: `help=` given to `add_parser` does not become `parser.description` — help-string tests must read the parent parser's `_choices_actions`.
