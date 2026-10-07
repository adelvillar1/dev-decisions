# Session recap — semantic embeddings lane (sem1) in dev-decisions, 2026-10-07

Plan: `docs/plans/2026-10-07-semantic-embeddings-lane.md` (approved and activated this session; dispositions recorded for the two standing flags). Library: [adelvillar1/sem1](https://github.com/adelvillar1/sem1) (born public this session).

## What shipped

**sem1 v0.1.0** (`60e3783` + `ff1bbea`): stdlib-only core (types contract, structurally-redacted JSONL telemetry, packed-float32 vector store with idempotent rebuild, pure cosine/top-k, provider registry), two providers — `llama-server` (OpenAI-style `/v1/embeddings` against the native LaunchAgent-owned server at `127.0.0.1:8901`, text-only by declaration) and `st-worker` (subprocess into in-repo `.venv-st`, sentence-transformers, text + images via the model-card interleaved format) — plus the capability probe with the opt-in mirror battery, a CLI (`embed`/`probe`/`doctor`), TECHNICAL-DOCUMENTATION.md (§1–§7), and the archify system diagram. 19 selftests green.

**dev-decisions** (this repo, `feature/semantic-lane` → merged): `_bootstrap_sem1` beside the sys1/sdm1 bootstraps; `scripts/devdec/semantics.py` with `semantic-index` / `semantic-dedup` / `semantic-nn` (all EVAL-ONLY, tagged `sem1_raw/...` in `providers_used`); `docs-gate --via-semantic` (k=4 section shortlist before the fan-out, logged per row); doctor row; `[sem1]` config block; README/SKILL/config documentation; 7 new selftests (48 total).

## Criterion evidence (C0–C9)

- **C0** embed through a real in-process HTTP server; unreachable endpoint raises `ProviderUnavailableError` naming the fix; telemetry redaction proven by grepping raw rows (no text, no key).
- **C1** `sem1 probe` reports text-only/768 dims live; image request raises `ProviderCapabilityError` before any I/O (unit + live).
- **C2** st-worker live mirror battery: pass — img_red→text_red 0.7170 > text_blue 0.6336, mirrored for blue (0.7109 > 0.6408), gradient prefers gradient text, images distinguishable (0.8826).
- **C3** parity over the sanity five: max pairwise cosine delta **0.0010** (< 0.01), ordering correct on both providers.
- **C4** store roundtrip bit-exact (`array('f')` bytes), manifest carries model/dims/count/sha256s (+op/ts meta), second rebuild zero duplicates (unit + live).
- **C5** `semantic-index` over the real stores: **31/6394 rows** have recoverable text (6363 redacted-only rows skipped, never guessed); fixture near-dupe pair with different sha256s reported by `semantic-dedup`; exact-sha join paths untouched.
- **C6** `semantic-nn` on this plan returns its own graded gate rows first, annotated with labels and disposition reasons (the waived UX-routes disposition surfaced with its text).
- **C7** docs-gate A/B on a fixture: baseline `PASS` == `--via-semantic` `PASS`; log row carries `via_semantic: {docs/storage-notes.md: [0,1,2,3]}` and `provider: sem1_raw+drex`; shortlist is log-only.
- **C8** socket-guard/hook-path tests: `cmd_scan_staged`/`cmd_classify_diff`/`cmd_zcode_gate` sources contain no sem1/semantic references; hooks untouched.
- **C9** sem1 TECH-DOC §1–§7 + archify `docs/architecture/system.{candidate.json,html,png}` (validate/deliver/check/browser-check all pass; one repositioning round attempted, reverted per contract; residual 4 advisory crossings documented); visual-judge review of the rendered PNG; devdec README/SKILL/config document the lane; recaps in both repos.

## Lessons

- `record()` must mkdir `path.parent`, not the log dir — the `YYYY/MM/DD.jsonl` layout has two levels below it (caught by the first selftest run).
- The devdec shim's `except Exception: pass` fallback silently converts a package-level crash into a monolith argparse rejection: the misleading "invalid choice" error was actually a `KeyError: 'op'` deeper in. Store manifests now carry per-entry metadata so dedup/nn never index missing keys.
- Pathlib shadowing: a package-level function named `probe` shadows the `probe` submodule; `from .probe import probe` then yields the module. Renamed the implementation `run_probe`.
- docs-gate rows land under `logs/YYYY/MM/DD/events.jsonl` — and "today" is UTC, so evening sessions write to tomorrow's folder.

## Standing limits

Eval-only until per-surface calibration floors exist (similarity thresholds don't transfer); batch-only (no hook path references this lane); no verdicts, joins, or verdict caches; audio/video accepted nowhere; hosted multimodal providers are a registry slot, not wired.
