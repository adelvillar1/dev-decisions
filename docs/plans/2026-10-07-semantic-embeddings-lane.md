---
status: active
created: 2026-10-07
updated: 2026-10-07
slug: semantic-embeddings-lane
---

# Plan: Semantic embeddings lane (sem1) for dev-decisions

## Context

dev-decisions has two model lanes: sys1 judges what the work says (diffs, plans, docs) and sdm1 scores what the work measures (run histories, calibration tables). Nothing indexes what the work *looks like*. The calibration JSONL stores are append-only and joined only by exact `input_sha256`, so a reworded diff or a re-committed change orphans its graded history; the gate fan-outs compare every criterion against every artifact with model calls; and the capture kit's visual loops have no cheap similarity signal in front of the expensive visual-judge. On 2026-10-07 a third lane was live-verified end to end on this machine: local EmbeddingGemma-2 (271M, omnimodal per model tags) served two ways — a native llama-server speaking OpenAI-style `/v1/embeddings` (text-only in practice, 46–86 ms; LaunchAgent-owned so the model auto-loads at login after a reboot) and the intended sentence-transformers path (text + image, deterministic re-encode, ~2 s per 2048px image), with a proven failure mode: servers return confident text-of-base64 vectors for image inputs with no error. That failure mode dictates the design — capability probes are first-class. This plan adds the `sem1` library (house pattern) and wires the first dev-decisions consumers so the calibration moat becomes searchable and gate fan-outs get cheap candidate shortlists.

## Approach

Reuse three proven seams instead of inventing new ones: sdm1's subprocess-venv worker pattern (`nvidia-sdm`) for the multimodal provider; sys1's registry + bootstrap pattern (`_bootstrap_sem1` beside `_bootstrap_sys1`/`_bootstrap_sdm1`); and the tabular lane's batch-only + eval-only discipline (cached vectors under `~/.local/share/dev-decisions/vectors/`, raw-tagged rows, no hook-path changes). The new repo `~/Projects/sem1` carries the library; dev-decisions carries the consumers.

**Decision-model check.** Embeddings are not a decision model and this plan adds no sys1 heads: the lane emits geometry (vectors, cosine, top-k), never verdicts. The operating rule is "embeddings propose, sys1/sdm1 and the JSONL loop dispose" — near-dupe confirmation, gate verdicts, and drift rulings stay with sys1 heads and the existing gate machinery; embeddings only rank, cluster, and retrieve candidates. Every graded similarity threshold in the consumers is eval-only until per-surface calibration floors exist (the `modernbert_raw` precedent; thresholds don't transfer, and this repo's own modernbert eval showed cosine-to-label-text barely separating labels at avg_top2 ≈ 0.01). The value lanes: displaced LLM spend (shortlisted fan-outs judge k candidates instead of all pairs), process time (nearest-graded-neighbor replaces re-deriving context), and accuracy (semantic dedup recovers graded history that exact-hash joins orphan).

**Standing hard rule (carried from the tabular lane):** no embedding network or model call ever happens inside pre-commit / pre-push / ZCode-gate synchronous paths in this phase — consumers read vectors cached by batch commands. Hook files are untouched.

**Wire facts this plan is built on (live-verified 2026-10-07):** the native llama-server (llama.cpp b11443) serves `unsloth/embeddinggemma-2-GGUF:BF16` at 768 dims, batch, 86 ms single / 46 ms batch-of-5, no `dimensions` truncation (400); the same model via sentence-transformers (`google/embeddinggemma-2`, torchvision required) produces real image embeddings — mirror cross-modal battery passes from both directions (img_red→text_red 0.7170 > text_blue 0.6336; mirrored for blue; gradient prefers gradient text), real archify diagrams separate correctly (architecture text 0.63 vs beach text 0.48), re-encode is deterministic at 1.0000, ~2.0–2.2 s per 2048×1320 PNG; the same server returns 4-decimal-identical text cosines to the HTTP path. Server image requests either 500 ("input (203341 tokens) is too large" = base64 tokenized as text) or silently embed the string — so the HTTP provider declares text-only and the probe enforces it.

High-level steps:

1. **sem1 scaffold (`~/Projects/sem1`):** stdlib-only core — `types.py` (normalized `EmbedResult`: vectors, dims, model, provider, telemetry; adding keys is legal, renaming is not), `telemetry.py` (JSONL rows: ts, op, provider, model, input_sha256, chars, dims, latency_ms, error_kind; never input text or key values), `store.py` (packed float32 vector files + JSON manifest keyed by `(provider, model, input_sha256)` under `~/.local/share/sem1/vectors/`), `similarity.py` (pure cosine, top-k), `registry.py` (`register()` extension point), `cli.py` (`embed`, `probe`, `doctor`).
2. **Provider `llama-server` (HTTP):** stdlib `urllib` against any OpenAI-style `/v1/embeddings` endpoint; on this machine the endpoint is the native llama-server at `http://127.0.0.1:8901`, owned by a LaunchAgent (`deploy/com.adelvillar1.sem1-llama.plist`, RunAtLoad + KeepAlive) so the model auto-loads at login after a reboot; no API key for the localhost binding (optional Bearer supported for hardened endpoints); batch; dims from response; 429/connection failures → `ProviderUnavailableError`; declares **text-only**.
3. **Capability probe (`sem1 probe`):** per endpoint — reachability, dims, declared modalities, and an opt-in semantic battery (the red/blue/gradient mirror test) that must pass in the expected direction before a provider may declare image capability. Server providers cannot opt into image capability unless the battery passes live; the documented wire-tell (image → ~203k text tokens) goes in TECH-DOC as the reason.
4. **Provider `st-worker` (multimodal):** subprocess worker into a durable venv (sdm1 `nvidia-sdm` pattern; venv at `~/Projects/sem1/.venv-st`, NOT `/private/tmp`; sentence-transformers + torchvision, model `google/embeddinggemma-2`); text and image via the `<|image|>` interleaved format; subprocess-per-invocation with ~9 s model load documented, batch-oriented (load once, embed the corpus).
5. **dev-decisions wiring:** `_bootstrap_sem1()` in `judgment.py` (env `DEV_DECISIONS_SEM1_PATH`, `~/Projects/sem1/src`, sibling search), `doctor` sem1 row, `[sem1]` block in `config.example.toml`, new `scripts/devdec/semantics.py` module, JSONL rows tagged provider `sem1_raw` until calibrated.
6. **`semantic-index`:** batch command rebuilding the vector index over the calibration stores (JSONL events + feedback rows with extractable text: diffs, plan criteria, gate claims), idempotent by `(input_sha256, model)`, manifest + packed vectors under `vectors/`.
7. **`semantic-dedup`:** near-dupe report over the calibration store — pairs above an eval-only threshold with their graded labels/dispositions side by side; report only, exact-sha joins unchanged.
8. **`semantic-nn`:** nearest graded neighbors for a target text/diff/file — prints the k nearest graded rows with labels and disposition pairing so new decisions inherit their graded context.
9. **`docs-gate --via-semantic` (eval-only):** embeds doc sections + claims once, retrieves the top-k sections per claim, and runs the existing sys1 fan-out over the shortlist only; JSONL rows carry the shortlist (ids + scores) and the `sem1_raw` tag; verdicts must be identical to the unshortlisted baseline on a fixture.
10. **Docs + diagrams:** sem1 is born with its contract docs (README, TECHNICAL-DOCUMENTATION.md §1–§7 skeleton, archify `docs/architecture/system.{candidate.json,html,png}`); dev-decisions README/SKILL.md/config.example.toml document the lane, the raw tag, and the batch-only rule; session recap in both repos at completion.

## Use cases

- [ ] A: As the owner of the calibration loop I want near-dupe inputs in the JSONL store surfaced with their graded labels side by side, so graded history stopped being orphaned by exact-hash joins.
- [ ] B: As a committer I want the nearest graded neighbors of a new diff or plan retrieved with their labels and dispositions, so every new decision inherits its measured context instead of re-deriving it.
- [ ] C: As a gate operator I want docs-gate's claim-vs-doc fan-out to judge a retrieved shortlist instead of every pairing, so the gate's token cost drops without changing its verdicts on fixtures.
- [ ] D: As a capture-kit operator I want sem1's st-worker provider to embed rendered-artifact images into a deterministic vector store (same render re-embeds at 1.0000 cosine), so the drift pre-filter integration that follows this plan can skip visual re-checks off that stored signal.
- [ ] E: As an sdm1 user I want embedded text fields (issue titles, check names) available as feature columns so tabular surfaces stop treating free text as high-cardinality categoricals (eval-only in this phase).
- [ ] F: As a maintainer I want fuzzy near-duplicate joins for override-prior reported eval-only, so renamed flaky tests and reworded inputs become visible without changing the exact-sha ground-truth join.

## UX routes

None — no `docs/ux/` route contracts exist in this repo and no rendered-app UI changes are in scope; the tower consumes the new JSONL rows and vector manifests through existing ingestion (no route contract change).

## Acceptance criteria

Order is identity: C0, C1, … in checkbox order; evidence-gate, plan-reconcile, and surfaces key on this order.

- [ ] C0: `sem1.embed()` through the `llama-server` provider completes a text batch end-to-end using only stdlib imports, raises `ProviderUnavailableError` naming the env var when the endpoint is unreachable, and its telemetry rows carry dims/latency/provider with no input text and no key values (redaction test proves both).
- [ ] C1: `sem1 probe` against the HTTP provider reports text-only with the observed dims, and an image request against it raises `ProviderCapabilityError` instead of returning a vector — the 2026-10-07 silent text-of-base64 failure is impossible by construction (unit test proves the raise).
- [ ] C2: `sem1.embed()` through the `st-worker` provider returns 768-dim vectors for text and for an image file, and the live mirror battery passes in the expected direction: each solid-color image is closer to its own color text from both sides and the gradient prefers the gradient text.
- [ ] C3: the same five sanity texts embedded via `llama-server` and via `st-worker` agree on ordering (auth pair cosine > auth/cake cosine on both) and on cosine value within 0.01, proving the two providers serve the same model behavior.
- [ ] C4: the packed-vector store roundtrips vectors bit-exactly (`array('f')` bytes equality), the manifest records model/dims/count/per-row sha256s, and a second rebuild over the same corpus writes zero duplicate entries.
- [ ] C5: `dev-decisions semantic-index` builds the vector index over the real calibration stores (every JSONL row with extractable text gets a vector), and on a fixture containing two near-identical inputs with different sha256s, `dev-decisions semantic-dedup` reports that pair while exact-sha join code paths are untouched.
- [ ] C6: `dev-decisions semantic-nn` on a fixture target returns the planted graded row first and prints that row's label and disposition-pairing fields in the output.
- [ ] C7: `dev-decisions docs-gate --via-semantic` on a fixture runs eval-only: its JSONL rows carry the `sem1_raw` tag and the logged shortlist, its gate verdict equals the baseline verdict from the same fixture without the flag, and a unit test proves no label-applying or blocking path is reachable from the semantic shortlist.
- [ ] C8: a socket-guard test proves only `sem1`/`semantic-*` code paths open embedding endpoints, and `grep` over the hook paths (scan-staged, classify-diff, zcode-gate) shows no sem1 references — hooks are byte-identical to before this work.
- [ ] C9: sem1 ships with README, TECHNICAL-DOCUMENTATION.md (§1–§7 skeleton including the wire-tell and capability-probe contract), and an archify `docs/architecture/system.{candidate.json,html,png}` that passes XML validation and a visual-judge review of rendered PNGs; dev-decisions README/SKILL.md/config.example.toml document the lane, the `sem1_raw` tag, and the batch-only rule; recaps are drafted in both repos at completion.

## Files to be touched

**sem1 (new repo `~/Projects/sem1`):**
- `src/sem1/types.py`, `src/sem1/telemetry.py`, `src/sem1/store.py`, `src/sem1/similarity.py`, `src/sem1/registry.py`, `src/sem1/probe.py`, `src/sem1/cli.py`, `src/sem1/__init__.py` — stdlib-only core
- `src/sem1/providers/http_llama.py` — OpenAI-style `/v1/embeddings` client, text-only declaration
- `src/sem1/providers/st_worker.py`, `src/sem1/providers/worker_st.py` — subprocess worker + host side (sentence-transformers, text + image)
- `pyproject.toml` — no runtime deps; `[st]` extra pins sentence-transformers/torchvision for the worker venv
- `tests/` — redaction, capability-raise, store roundtrip, parity (stubbed worker), shortlist tests
- `README.md`, `TECHNICAL-DOCUMENTATION.md`, `docs/architecture/system.candidate.json` (+ built html/png), `docs/plans/` copy of the execution-era plan updates
- `deploy/com.adelvillar1.sem1-llama.plist` — LaunchAgent recipe for the native llama-server (RunAtLoad + KeepAlive; model auto-loads at login after a reboot; installed copy at `~/Library/LaunchAgents/`)

**dev-decisions (`~/Projects/dev-decisions`):**
- `scripts/devdec/judgment.py` — `_bootstrap_sem1()` sibling bootstrap
- `scripts/devdec/semantics.py` — new module: `semantic-index`, `semantic-dedup`, `semantic-nn`, `docs-gate --via-semantic` shortlisting
- `scripts/devdec/cli.py`, `scripts/devdec/config.py`, `config.example.toml` — subcommands, `[sem1]` block, raw-tag filter
- `scripts/devdec/doctor.py` (or its current home) — sem1 availability row
- `scripts/selftest.py` — fixture + socket-guard tests for the new surfaces
- `README.md`, `SKILL.md` — lane documentation, batch-only rule, raw-tag filter

## Out of scope

- Hook-path embedding calls (batch-only rule; revisit only with measured local latency and a passed calibration floor).
- Hosted multimodal embedding providers (Cohere/Jina/Voyage) — the registry slot exists, wiring does not.
- Capture-kit / ux-gate integration of the drift pre-filter (this plan ships the similarity capability; the loop integration is its own plan).
- sdm1-side consumers (feature columns, fuzzy override-prior joins) — ships as eval-only reports here, real joins in a follow-up plan.
- Audio and video modalities, semantic caches over verdicts (never: graded-equality requirement plus the caching-erodes-gains finding), and similarity-gate calibration floors (requires accumulated graded rows first).

## Verification

- C0/C1/C4: `python -m unittest` in sem1 (redaction, capability-raise, store roundtrip, idempotent rebuild fixtures).
- C2: live `sem1 probe --provider st-worker --battery` against the durable venv; expected-direction gaps printed and asserted.
- C3: live parity script embedding the five-text fixture through both providers; ordering + ≤0.01 cosine asserted.
- C5/C6: fixture corpora under `tests/fixtures/`; run `dev-decisions semantic-index` then `semantic-dedup`/`semantic-nn`; planted-pair and planted-neighbor assertions in `scripts/selftest.py`.
- C7: fixture plan + docs corpus; run `docs-gate` with and without `--via-semantic`; diff verdicts (must be equal); inspect JSONL rows for `sem1_raw` + shortlist fields; unit test asserts no blocking path reachable.
- C8: socket-guard test (patched socket capturing connects during a hook-path invocation); `grep -r sem1 scripts/devdec/{workflow,gitops}.py` returns nothing.
- C9: archify finalize gates + `documents:visual-judge` on rendered PNGs (light+dark, per the 2026-10-07 precedent); README/SKILL greps for the lane, tag, and batch-only rule; recaps in `docs/recaps/` of both repos.

## Linked artifacts

- `~/Projects/sem1/TECHNICAL-DOCUMENTATION.md` — new repo, contract doc born with the code (§1 architecture, §2 providers, §3 wire + capability probe, §4 store format, §5 telemetry, §6 CLI, §7 limits)
- `~/Projects/sem1/README.md` — lane description, quickstart, probe-first usage
- `~/Projects/dev-decisions/README.md` + `SKILL.md` — semantic lane section: commands, `sem1_raw` filter, batch-only rule
- `~/Projects/dev-decisions/config.example.toml` — `[sem1]` block
- `docs/recaps/` in both repos — session recaps at completion
- `docs/architecture/system.candidate.json` (sem1) and dev-decisions' archify diagram — add the semantic lane

## Risks

- sentence-transformers + torch download is multi-GB and HF unauthenticated downloads are rate-limited — mitigate with a durable venv path and a documented HF_TOKEN note.
- The `/private/tmp` venv wipe gotcha (2026-10-07) is why the worker venv lives in the repo tree, not tmp.
- Shortlisting could drop a doc section that would have flagged — C7's verdict-equality fixture is the guard, and the shortlist is logged so misses are auditable.
- Provider drift: the HTTP lane's behavior is build-specific (mmproj claims, dims truncation) — the probe runs per endpoint, not per model name.
- dev-decisions still has no TECHNICAL-DOCUMENTATION.md (standing conventions gap), so arch-gate cannot run against the devdec-side steps; sem1 gets its contract doc at birth, where arch-gate can run during execution.

## Dependencies

- sys1 (`~/Projects/sys1`) — bootstrap + registry precedent (imported, stable).
- sdm1 (`~/Projects/sdm1`) — venv-worker pattern precedent (pattern reuse, no code dependency).
- dev-decisions `main` — consumer repo (tabular lane merged at `d8de96a`).
- External: `google/embeddinggemma-2` on HF (downloads at first worker run), the native llama-server LaunchAgent for the HTTP provider (recipe in `deploy/`; auto-loads at login, `launchctl kickstart -k gui/$(id -u)/com.adelvillar1.sem1-llama` to restart by hand).
