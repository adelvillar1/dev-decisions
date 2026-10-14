# Plan — dev-decisions blend1 synthesis lane (explain-the-gate loop)

```yaml
---
status: draft
created: 2026-10-14
updated: 2026-10-14
slug: blend1-synthesis-lane
---
```

## Context

dev-decisions is the workflows-and-loops layer over the lane substrate. Its
lanes today: **sys1** judges what the work says (route/gates), **sdm1**
scores what it measures (tabular batch), **sem1** indexes what it looks like
(semantic batch — `semantic-index`/`dedup`/`nn`, `docs-gate --via-semantic`
shortlist, all landed 2026-10-07), **gen1** renders (media batch). None of
them **composes an answer** — every loop ends in a verdict a human
interprets.

**blend1 is the fifth lane** (shipped in blend1 2026-10-14): one `ask()` —
d1 route gate → parallel fact lanes → LFM2.5-24B synthesis → GLiDE
validation gate — returning a fact-grounded, validated answer, with a
givens contract (facts never invented; "insufficient facts" is a first-class
outcome) and per-project corpora with docs-derived refresh.

**First loop: explain-the-gate.** Every verdict-emitting gate gains a second
beat: a synthesized, validated *explanation* of the verdict, riding in the
JSONL beside it, keyed by `input_sha256`. This upgrades the calibration
dataset itself — rows currently carry nouls and labels; with explanations
they carry readable causes, and floors get fitted against reasons, not just
scores.

### Verified current-state facts (read this session)

- Use case C of `2026-10-07-semantic-embeddings-lane.md` is **already
  built**: `docs-gate --via-semantic` shortlist (`gates.py:969-1001`),
  `sem1_raw` tag, C7 verdict-equality, C8 socket guard. **blend1 appears
  nowhere in the repo** — greenfield.
- No `pyproject.toml`, no `.venv`: siblings import lazily inside functions
  (`judgment.py:43 import sys1`, `:86 import sdm1`) with
  `# type: ignore[import-not-found]` — the pattern for blend1.
- `config.py:179 log_record` appends arbitrary dicts to
  `~/.local/share/dev-decisions/logs/YYYY/MM/DD/events.jsonl` (ts defaulted,
  never raises).
- Lane module pattern (`semantics.py`, `media.py`): module docstring states
  the operating rule + raw tag + batch-only discipline; CLI subcommands
  registered in `cli.py` via `sub.add_parser` + `set_defaults(func=...)`;
  the media lane has `cmd_media_budget` — the spend-visibility pattern to
  mirror.
- Recoverable-text pattern (`semantics.py`): JSONL rows are redacted (no
  input text), so text artifacts live on disk keyed by `input_sha256`
  (`LOG_DIR/feedback/feedback.jsonl` is the sibling example).
- blend1 cost profile (measured live 2026-10-14): synthesized ask ≈ $0.001
  (24B) + ≈ $0.0005 (GLiDE gate); direct-path ask ≈ $0.000004.

### Documented defaults (resolved at draft; revisit at plan approval)

1. **Operating rule**: BLEND1 PROPOSES PROSE, THE GATE DISPOSES. The lane is
   **eval-only** (`providers_used` tag `blend1_raw` — explanations are not
   calibrated; nothing here gates, blocks, or joins) and **batch-only** (no
   command may run inside a synchronous hook path; hooks never import the
   module — socket-guard test proves it, mirroring C8).
2. **Artifact integrity**: `explain` takes `--artifact PATH` — the same
   input the gate judged (a diff, a plan file). The command hashes it
   (sha256) and **refuses with a named reason when the hash does not match
   the row's `input_sha256`** — explaining a drifted artifact is worse than
   not explaining. Rows whose artifact is unrecoverable are counted and
   skipped, never guessed at (the semantics.py discipline).
3. **Explanation storage**: text never enters the JSONL (redaction is the
   house rule). It lands at
   `LOG_DIR/feedback/explanations/<gate_input_sha256>.md`; the row carries
   `explanation_sha256`, `explanation_chars`, the ask's route/gate
   telemetry, and the linked `input_sha256` — recoverable-text pattern, so a
   future semantic index can embed explanations like feedback notes.
4. **Row shape**: `{"op": "explain", "input_sha256": <gate row's>,
   "explained_op": <the gate row's op>, "provider": "blend1",
   "providers_used": [..., "blend1_raw"], "route": {...}, "validation":
   {...}, "explanation_sha256": ..., "explanation_chars": ...}`.
5. **Spend visibility**: `dev-decisions synthesis-budget` mirrors
   `cmd_media_budget` — reads the `blend1_raw` rows and reports ask counts +
   estimated spend from blend1's per-row cost fields. Eval-only spend stays
   visible.
6. **Install**: `pip install -e ~/Projects/blend1` into the ambient python
   (the siblings' install style); `--extra web` optional (the web lane only
   fires when d1's `needs_live_facts` clears the floor — gate explanations
   shouldn't need it). Missing import → fail-open naming the install, same
   as every other optional sibling.

## Approach

### Phase 1 — the lane module

1. `scripts/devdec/synthesis.py` — the blend1 wrapper: module docstring with
   the operating rule + `blend1_raw` tag + batch-only rule (mirror
   `semantics.py`'s docstring register); lazy `import blend1` with fail-open
   naming the install; `explain_gate_row(row, artifact_path)` — verify the
   artifact hash (default #2), build the ask prompt from the row's verdict
   fields + the artifact, call `blend1.ask()`, write the explanation file
   (default #3), return the row dict (default #4). Never raises.
2. Budget reader: `synthesis_budget()` — aggregate the `blend1_raw` rows.

### Phase 2 — CLI

3. `cli.py`: `dev-decisions explain --artifact PATH (--row SHA |
   --last-failed | --date YYYY-MM-DD) [--dry-run]` — resolves the target
   gate row(s) from the JSONL, runs the loop, prints the explanation +
   writes the file + appends the row. `--dry-run` resolves and hashes but
   makes no ask (spend-free rehearsal). `dev-decisions synthesis-budget`.

### Phase 3 — tests + docs

4. Tests (offline, stub blend1 via `sys.modules` fake exposing `ask()`):
   artifact-hash mismatch → refuse with reason, no ask, no row; matching
   hash → explanation file written, row appended with the right shape;
   missing blend1 import → fail-open naming the install; eval-only unit
   test proving **no verdict-applying or blocking path is reachable** from
   an explanation (C7's discipline); socket-guard test proving the hook
   paths (scan-staged, classify-diff, zcode-gate) never import synthesis
   (C8's pattern); budget aggregation over fixture rows.
5. Docs: README provider-table row (blend1 lane: synthesis, eval-only,
   batch-only), SKILL.md (the `explain` + `synthesis-budget` commands),
   config.example.toml if it enumerates lanes, this plan, recap.

## Acceptance Criteria

- [ ] **AC1** — offline: `explain` with an artifact whose sha256 matches the
  gate row's `input_sha256` writes
  `logs/feedback/explanations/<sha>.md` and appends an `op: "explain"` row
  carrying `explanation_sha256`, `explanation_chars`, the `blend1_raw` tag,
  and the linked `input_sha256` (pytest, stubbed blend1).
- [ ] **AC2** — offline: artifact hash mismatch → named refusal, no ask, no
  row (pytest).
- [ ] **AC3** — offline: missing blend1 import → fail-open error naming
  `pip install -e ~/Projects/blend1` (pytest).
- [ ] **AC4** — offline: a unit test proves no verdict-applying or blocking
  path is reachable from an explanation row (pytest).
- [ ] **AC5** — offline: socket-guard test proves only `blend1`/`synthesis`
  code paths import blend1, and grep over the hook paths shows no synthesis
  references — hooks byte-identical (pytest + grep).
- [ ] **AC6** — offline: `synthesis-budget` over fixture rows reports ask
  counts + estimated spend (pytest).
- [ ] **AC7** — live: `dev-decisions explain --artifact <a real judged
  artifact> --row <sha>` on a real gate row produces a validated
  explanation file; the ask's gate outcome rides in the row (parent-run —
  ~$0.002).
- [ ] **AC8** — `pytest` green (existing + new) and docs updated per Phase
  3 item 5 (grep `blend1` in README/SKILL.md).

## Files to be Touched

**New**
- `docs/plans/2026-10-14-blend1-synthesis-lane.md` (this plan)
- `scripts/devdec/synthesis.py` — the lane module

**Modified**
- `scripts/devdec/cli.py` — `explain` + `synthesis-budget` subcommands
- `tests/` (the dev-decisions test layout — implementer locates the right
  file; the socket-guard sibling test is the pattern)
- `README.md` — provider table row
- `SKILL.md` — the new commands
- `config.example.toml` — if it enumerates lanes

**Deleted**
- none

## Out of Scope

- Web-verified gates (the other aggregated shape — web lane × docs/evidence
  gates; separate plan).
- Dashboard-askable (blend1 × dashboard query loop; separate plan).
- Per-project corpora + warmup freshness inside dev-decisions (the blend1
  feature exists; wiring dev-decisions' own history to it is separate).
- Calibration floors for explanations — eval-only (`blend1_raw`) until rows
  exist; fitting them is a later, evidence-backed step.
- blend1 in any hook path — forbidden by the batch-only discipline.

## Verification

```bash
# offline gate (implementer)
python -m pytest tests/ -q            # existing + new synthesis tests
python scripts/dev_decisions.py explain --artifact docs/plans/2026-10-07-semantic-embeddings-lane.md --dry-run
python scripts/dev_decisions.py synthesis-budget
# live (parent)
python scripts/dev_decisions.py explain --artifact <real judged artifact> --row <gate input_sha256>
cat ~/.local/share/dev-decisions/logs/feedback/explanations/<sha>.md
```

Expected: dry-run resolves + hashes with no spend; the live run writes the
explanation file and an `op: "explain"` row with the gate's outcome; hooks
are byte-identical (socket-guard + grep).

## Linked Artifacts

- `docs/plans/2026-10-07-semantic-embeddings-lane.md` (the lane pattern this
  mirrors: module docstring discipline, raw tag, C7/C8 test shapes)
- `docs/plans/2026-10-07-tabular-decision-lane.md`,
  `docs/plans/2026-10-08-media-generation-lane.md` (batch-lane siblings)
- blend1 repo: `~/Projects/blend1` (the lane; its web-lane and
  project-corpora plans of 2026-10-14)
- dev-decisions recap at completion (this repo's convention)

## Risks

| Risk | Mitigation |
|---|---|
| Synthesis spend in an eval loop (~$0.0015/ask) | Eval-only + `synthesis-budget` visibility; `--dry-run` rehearses spend-free; direct-path asks cost $0 when d1 routes low |
| Explaining a drifted artifact (the file changed since the gate judged it) | Hash gate: mismatch → named refusal (AC2); the row links by `input_sha256` |
| Explanation hallucination | blend1's GLiDE gate rates every answer before it ships; `blend1_raw` tag + eval-only until floors; the explanation never gates anything itself (AC4) |
| Hook-path contamination (latency/secrets in pre-commit) | Batch-only rule + socket-guard test + hook grep (AC5); hooks byte-identical |
| Explanation text leaking into the redacted JSONL | Text never logged — file on disk keyed by sha; the row carries hashes + counts only (default #3) |
| blend1 uninstallable in the ambient python (its `web` extra needs py≥3.11) | Core import needs no extra; `--extra web` optional; missing import fails open naming the install (AC3) |
