---
name: dev-decisions
description: Use decision models (Jev, GLiNER-2.5-Decide) to classify diffs, scan commits for secrets/PII, gate git operations, PRs, and issues in dev workflows. Use when the user mentions dev-decisions, decision-model gates, pre-commit scans, diff classification, Jev for code review, GLiNER for secrets, decision-model workflows, decision logging, PR gating, issue triage, changelog generation, or ZCode agent routing. Triggers on: "scan the staged diff", "classify this commit", "what type of change is this", "install dev-decisions hooks", "decision log", "calibration data for thresholds", "PR gate", "triage issues", "generate changelog", "ZCode gate".
---

# dev-decisions: decision-model gates for git + ZCode workflows

A stdlib-only Python CLI (`dev-decisions`) — the verification and calibration layer for AI-aided development: classifies diffs, scans for secrets/PII, gates PRs/issues/plans/evidence/docs/architecture, and logs every decision to JSONL as a calibration set for fitting thresholds (and earning autonomy) later.

## Quick start

```bash
# Fleet management
dev-decisions status                    # all repos under ~/Projects
dev-decisions bulk-install              # install hooks in every repo
dev-decisions bulk-install --force      # overwrite existing hooks
dev-decisions status --root ~/code      # scan a different root

# PR gating
dev-decisions pr-gate [branch] --dry-run
dev-decisions pr-gate 123 --provider local
dev-decisions pr-gate --fanout --dry-run     # per-file heads packed into ONE request
dev-decisions pr-gate --diff-file patch.diff --fanout   # offline; never applies labels

# Plan gating (plan-as-contract)
dev-decisions plan-gate docs/plans/2026-10-02-feature.md
dev-decisions plan-gate plan.md --criteria-file requirements.txt

# Evidence gating (QA close-out)
dev-decisions evidence-gate plan.md closeout-evidence.md

# Architecture gating (plan vs documented architecture)
dev-decisions arch-gate docs/plans/2026-10-02-feature.md

# Record the human decision on a negative gate verdict
dev-decisions disposition plan-gate docs/plans/2026-10-02-feature.md --status waived --reason "criterion 6 deferred to the follow-up plan"

# Issue triage (dry-run by default)
dev-decisions triage-issues [owner/repo] --limit 20
dev-decisions triage-issues --state all --dry-run

# Changelog generation
dev-decisions changelog --since v0.1.0
dev-decisions changelog --since v0.1.0 --write
```

## Architecture

Three layers, each independently useful:

| Layer | What it does | When it runs |
|---|---|---|
| **git hooks** | `pre-commit` → `scan-staged` (block secrets); `pre-push` → `classify-diff` | Automatic, per-repo |
| **ZCode hook** | `PreToolUse` on Bash → detects destructive commands, gates `git commit`/`git push` | Automatic, global (opt-in) |
| **CLI** | All subcommands, invocable from ZCode skill or terminal | On-demand |

Shared JSONL log at `~/.local/share/dev-decisions/logs/YYYY/MM/DD.jsonl` is the calibration dataset.

## Fleet view

`status` scans a root directory (default `~/Projects`) for git repos and reports:
- Hook installation state (managed by dev-decisions vs foreign/absent)
- Sensitivity (`.dev-decisions.toml sensitive = true` or `.env` with DB/auth markers)
- Env file presence
- Project type (js/ts/python/other)

`bulk-install` installs hooks in every repo under the root. Sensitive repos get flagged with `[sensitive]`. Skips already-installed repos unless `--force`.

### New project setup (run for every new/cloned repo)

1. `dev-decisions install-hooks <repo>` — installs pre-commit (scan-staged, local, no keys) and pre-push (classify-diff) from `hooks.*` config. Hooks source the machine-global key file, so a fresh repo needs no per-repo config.
2. Keys: one file, `~/.config/dev-decisions/env` (`chmod 600`, `FASTINO_API_KEY=...` / `TYPESAFE_API_KEY=...` lines) — created once per machine; `install-hooks` wires every managed hook to source it with `set -a`. Never commit it, never put keys in the hook file itself.
3. Nothing else. The hook classifies every push through sys1's router (`auto`: the roster chain glide→drex→jev from sys1 config); without keys it degrades to warn-and-proceed (fail-open by design); sensitive repos skip vendor calls unless `--allow-vendor`.

Use this when onboarding a new machine (keys file + `bulk-install`), after cloning a batch of repos, or when adding a project to your `~/Projects` directory.

## Provider routing

**Default provider is `auto` (2026-10-02): every command routes through sys1's router** — the capacity gate, per-task overrides, and the roster chain (`glide,drex,jev`) all live in sys1's config, so dev-decisions tracks the roster without code changes here. Explicit ids still pin a provider. Two measured facts make routing load-bearing: **hosted Decide rejects diffs past its 8192-token context** (`input_too_long` — long diffs must ride glide/drex/jev), and **GLiDE times out server-side on multi-head fan-out over large states** (hence `pr_gate_fanout = "jev"` by_task override in `~/.config/sys1/config.toml`). Without sys1 installed, auto falls back to decide. Local GLiNER stays supported-by-design, offline/private repos only. Roster and measured provider facts: the `sys1-provider-pool` skill.

| Job | Provider | Why |
|---|---|---|
| Diff classification (default) | **decide** — hosted Fastino API (`fastino/GLiNER2.5-Decide` via sys1) | The active fastino provider; fast, declines on ambiguity; the `decide` id means the HOSTED wire — the Decide model is what `local` runs |
| Calibrated judgments, multi-question | **jev** (`jev-1.13.0`) | Choice/Score/Noul wire shape is documented; $0.042/M input, output free; **Decision Index 0.2 (independent, 40 benchmarks)** scores it 51.67 vs Decide's 9.98 |
| Offline / private repos only | **local** (supported, not encouraged) | Same Decide model offline; use for sensitive repos instead of `--allow-vendor` |
| Agreement / confidence gating | **both** (`decide+jev` via sys1) | Capture disagreements for calibration |

## Provider facts (verified 2026-09-27)

- **Jev confidence is a distribution-shape statistic, not raw max probability.** For N options the docs-state approximation is `(N × max_prob − 1) / (N − 1)`. Binary collapses to max; 3+ diverges. Do not treat `confidence == probabilities[winner]` in code — they are different numbers with different semantics.
- **Jev is trained with RLCD — Reinforcement Learning for Calibrated Decisions** (not "reinforcement contrastive distillation"). No RLCD paper exists; TypeSafe has published an objective and data source but not the method.
- **Hosted Decide does not support constrained classification** (`GET /v1/base-models` shows `encoder_features: ["classifications","entities","relations","structures"]` only). Cross-task rules are only reachable via the local `Classifier` path or by hosting `fastino/gliner2.5-multi-v1` instead.
- **Hosted Decide description-map labels are rejected.** The API takes flat `labels` arrays only; embed rubric descriptions in the task prose. The open-source SDK silently drops dict labels on the hosted path.
- **Warming on Fastino is HTTP 425, not a `model_warming` field.** The old `is_warmup` field is removed. Retry 425/429/503 with `Retry-After`, set read timeout ≥300 s.
- **GLiNER2.5-Decide is a SpanExtractor despite the "2.5" name** (fine-tuned from `gliner2-large-v1`, max_width 8, DeBERTa-v3-large). Only `GLiNER2.5-multi-Decide` and pure `gliner2.5-*` models use the boundary architecture. Its safetensors reports 486,444,053 params; Fastino's headline "340M" omits embedding tables.

## Task registry

Every feature is a named task with provider-specific heads. Add new tasks by defining heads — no provider-code changes.

| Task | Auto-detected when | Use |
|---|---|---|
| `change` | default | Diff type + risk |
| `commit_audit` | always | Message accuracy |
| `deps_risk` | deps files touched | Bump level + breaking |
| `docs_drift` | docs-only diff | Behavior change + docs updated |
| `api_drift` | always | Public API + breaking + severity |
| `pr_gate` | `pr-gate` command | Change type + risk + labels |
| `issue_triage` | `triage-issues` command | Kind + priority |
| `safety` | `zcode-gate` destructive patterns | Destructive + reversible |

## classify-diff heads

Decide (flat labels, Pioneer schema):
- `diff_type`: feat / fix / refactor / docs / test / chore
- `risk`: low / medium / high

Jev (criteria as first-class descriptions):
- `diff_type`: Choice with criteria
- `risk_tier`: Choice with criteria
- `breaking_change`: Noul
- `personal_data`: Noul

Local GLiNER (same heads as Decide, via existing `/private/tmp/gliner-decide` venv):
- `diff_type`: feat / fix / refactor / docs / test / chore
- `risk`: low / medium / high

Both providers return confidence. The JEV-as-a-Judge paper's finding applies: **confidence is an escalation signal, not a certificate**. Default floor is 0.7; null/declined verdicts always escalate. But the two providers' confidences mean different things — Jev's is the concentration statistic `(N × max_prob − 1) / (N − 1)` (not raw max probability), while Decide returns the winning label's probability directly. **Do not threshold them at the same number without local calibration**; the CMU paper measured AUROC 0.869/0.745/0.863 for Jev across workloads, meaning the *ordering* of confidences is meaningful but the *absolute* number is not transferable — fit per-provider per-head floors from the JSONL log.

## PR gating

`pr-gate` classifies a PR diff and applies labels via `gh pr edit --add-label`. Local-first; dry-run by default.

```bash
dev-decisions pr-gate [branch] --dry-run
dev-decisions pr-gate 123 --provider local
dev-decisions pr-gate --fanout --dry-run
dev-decisions pr-gate --diff-file patch.diff --fanout
```

**`--fanout` (speculative fan-out):** packs a risky Noul + action Choice per changed file (cap 12, largest chunks first) into the SAME request as the standard diff_type/risk_tier/labels heads — 19 heads in one call, measured *faster* than the 3-head call, with per-file verdicts printed. Requires sys1; routes to `jev` via by_task override. `--diff-file` reads any unified diff offline and never applies labels. Overall labels always come only from the three standard heads — per-file action choices never become PR labels.

Labels applied: `bug`, `feature`, `refactor`, `docs`, `chore`, `test`, `ci` (multi-label).

## Plan gating

`plan-gate` runs the plan-as-contract check on a plan markdown file: per acceptance criterion, is it **covered** (noul) and **verifiable** (observable/partial/unverifiable choice); per work section, is it **in scope** (scope-creep check). Criteria are the plan's checkbox lines (`- [ ]`/`- [x]`) or a `--criteria-file`; sections are `##`/`###` headings minus meta sections (outcome, verification, files-to-touch, etc.). One speculative fan-out request; routes to `jev` via by_task override. Exit 1 when gaps exist, 0 when clean.

```bash
dev-decisions plan-gate docs/plans/2026-10-02-feature.md
dev-decisions plan-gate plan.md --criteria-file requirements.txt
dev-decisions plan-gate plan.md --tests tests/    # + criteria-vs-test-suite coverage
```

**`--tests <root>` (test coverage):** mechanically collects pytest-style test files (`test_*.py`/`*_test.py`, test function names) under the root, then matches each criterion to the suite in the same request — matched criteria print `C<i> -> <test file>`, unmatched print UNTESTED. Operational criteria (copy a key, run a live check) and retrospective criteria (record results in the Outcome) correctly come back UNTESTED: that is the manual/E2E bucket, not a failure. Counting is code; the model only matches meaning. Mechanical line/branch coverage remains the floor — this gate judges semantic coverage, and a criterion matched to a vacuous test is a known blind spot (assertion-depth checking is future work).

**Coverage ≠ correctness:** a flag is a review trigger, not a veto — borderline probabilities (roughly 0.3–0.6) mean escalate to human, and the gate is advisorial by design until floors are fitted from JSONL outcomes (`op: "plan-gate"` rows, hand-graded; ~20 plans to calibration).

**Self-consistency (2026-10-02):** plan-gate merges 3 draws per head (`--draws`); flags carry mean confidence and an `/UNSTABLE` marker when draws straddle the cut — the fix for the graded jaggedness finding (3 single-draw runs, 3 different flag sets on the identical plan).

**Dispositions write their own calibration rows:** since 2026-10-02, `dev-decisions disposition` looks up the matched gate event (within 30 days) and writes per-head feedback rows automatically — fixed/waived grade the prediction correct, overridden inverts it. Gate rows now carry `input_sha256` + per-head `heads`, so pairing works; pre-enrichment rows are skipped.

**Mechanical pushdown:** a diff whose changed paths are ALL doc files gets its drift noul forced to no in code (not the model), and task routing reads changed paths instead of diff body text (a code diff mentioning `.md` no longer routes to docs_drift).

## UC corpus (issues vs existing functionality vs planned design)

`uc-gate <issues.md> --fs FUNCTIONAL-SPECIFICATIONS.md --design plan.md`: the per-issue 2x2 coverage matrix (reinvention / already-solved / extension / genuine-new / residual-gap / true-gap) from two citation-forced coverage fan-outs — one against existing documented functionality, one against the planned design's mechanisms. Issues contract: `## Issues` with `- [ ] <id>: statement` checkboxes. Coverage cites PROBABILITIES not labels (jev-family wires ignore multi-label; the cite floor defaults 0.15). `auto` routes through sys1. **Deployment constraint learned in probe (2026-10-03): the FS/design inventories must PREDATE the design being judged — a retroactive run against a post-hoc FS reads the plan's own output as "existing" and reports reinventions (citation precision was 12/12; quadrants 0/6, artifact). Valid mode = design-time, before the FS absorbs the new functionality.**

## UX corpus (ux-surface + ux-gate)

The UX design corpus: `docs/ux/<route>.md` contracts (states matrix + control inventory) are verified against rendered captures from the ux-capture kit (workspace/ux-capture-probe: playwright pixels + DOM dump + a VLM capture layer whose output triangled against the DOM — VLM-vs-DOM disagreement is a perception flag, DOM-vs-spec an implementation gap). `ux-surface <app>` builds the routes x states x verification artifact (confirmed-drift / flags-ungraded / clean / unverified); `ux-gate <app>` gates it: human-graded confirmed drift fails, ungraded flags and unverified states warn, semantic state-match stays WARN-only until its floor is fitted (Phase 0: 2/5). State names may contain hyphens (`has-data`); capture cell states normalize (`real` -> `has-data`).

```bash
dev-decisions ux-surface my-app --ux-docs docs/ux
dev-decisions ux-gate my-app
```

## Plan surface (feed-forward decomposition input)

`plan-surface` is `plan-gate`'s feed-forward counterpart: instead of checking a drafted plan, it precomputes the judgment graph the decomposer should assemble against. Layers, each one request (or chunked requests): criterion → module mapping (a choice head over a numbered repo inventory plus an existence noul, per criterion), pairwise criterion ordering (one choice head per unordered pair: `cI_first` / `cJ_first` / `independent`), then plain-code assembly — thresholded DAG, topological order, in-band edges reported as UNCERTAIN (never silently dropped), weakest-edge cycle breaks, path-pattern risk flags (migrations, auth, config, …). The artifact lands in `~/.local/share/dev-decisions/surfaces/<plan-stem>.surface.json`.

```bash
dev-decisions plan-surface docs/plans/2026-10-02-feature.md --repo-root .
```

Advisorial, never a gate: the decomposer may overrule any edge or mapping; record overrules with `dev-decisions disposition plan-surface <plan> --status overridden --reason ...` so they double as calibration rows. The predicted order is printed as SOFT (one linearization of the edges — a near-chain over 9+ criteria is the over-serialization signature, and the edge list with confidences is the actual signal). Layers route via by_task overrides `plan_surface_map` / `plan_deps` (both `jev`: the 2026-10-02 probe showed jev returns the full distributions the map choice needs, where drex/glide return one weak top-1 pick, and the deps fan-out is jev-native). Thresholds live in sys1 `plansurface` (`MAP_FLOOR` 0.3 recall-oriented, `DEP_THRESHOLD` 0.8 where probe direction-precision hit 1.0); `--map-floor/--map-top/--dep-threshold/--band` override.

## Plan reconcile (forward validation)

`plan-reconcile <plan.md>` grades a plan's stored surface artifact against what actually happened: the commit sequence since plan creation is ground truth for order (Kendall tau) and edge direction, with a sys1 attribution layer mapping commits to criteria when implementations share files. Writes `plan_surface_map`/`plan_deps` feedback rows. Run it the day a plan completes.

```bash
dev-decisions plan-reconcile docs/plans/2026-10-02-feature.md --repo-root .
```

## Calibration report (when to fit floors)

`dev-decisions calibration` reads both feedback stores (dev-decisions + the sys1 log dir where manual grading sessions land) and reports per (task, head, provider): graded count, confidence span, binned accuracy, and whether the head qualifies for a floor fit (>= 20 rows spanning >= 0.4). The 2026-10-02 answer was "none qualify" — now it is a lookup, not a judgment call. `--format json` for machines.

## Evidence gating

`evidence-gate` is the QA close-out check: does the collected evidence support a pass per acceptance criterion? A green exit code is weak evidence; this gate grades the bundle. Criteria come from the plan's checkboxes (order defines C<i>); evidence is a plain file with blocks tagged `== C0 ==`, `== C1 ==`, … containing commands, log tails, and full logs welcome — blocks bound at ~20k chars, total state bound at Drex capacity (~400k chars), so no tail-pasting.

```bash
dev-decisions evidence-gate docs/plans/2026-10-02-feature.md closeout-evidence.md
```

Per criterion with evidence: **sufficiency** noul (does it demonstrate the observable outcome), **consistency** noul (free of contradictions — skips, wrong build, errors the summary ignored, retried-until-pass), and a verdict choice (supported / insufficient / contradicted). Criteria without an evidence block come back NO EVIDENCE. Routes `drex,jev` via by_task override (long-input corpus); `DREX_API_KEY`'s canonical home is the sys1 repo `.env`, with a consumer copy in `~/.config/dev-decisions/env`.

**Fail-closed rules:** missing evidence is never a pass; the gate downgrades passes but never overturns a deterministic failure (a red test stays red — this judges evidence, not the test). A strong inconsistency or insufficiency signal downgrades the verdict choice in code, never the reverse (choice and nouls can disagree — no structural invariants). Advisorial until calibrated; JSONL rows (`op: "evidence-gate"`) are hand-graded when later evidence contradicts a SUPPORTED verdict.

## Architecture gating

`arch-gate` asks: is the plan introducing anything that goes against the architecture documented in the project's TECHNICAL-DOCUMENTATION.md? Claims come from the plan's approach/phases/files sections; the rubric is the tech doc's sections. Per claim: **documented** noul (does the doc address this area), **conflict** noul (does the documented architecture contradict the claim), and a verdict choice (conforms / drifts / undocumented).

```bash
dev-decisions arch-gate docs/plans/2026-10-02-feature.md
dev-decisions arch-gate plan.md --tech-doc docs/TECH-DOC.md --repo-root ~/Projects/my-repo
```

Verdicts are derived in code, fail-closed: low documented → **UNDOCUMENTED** (a conventions gap in the tech doc — never a drift accusation without a written rubric); high conflict downgrades a conforms verdict to **DRIFTS** (exit 1, human review required). Undocumented claims are documentation findings, not plan defects. The judge reads the doc, never code — conformance is against the documented architecture, and where the doc is stale, that contradiction is itself the finding.

## Issue triage

`triage-issues` batch-classifies issues and applies labels. Dry-run by default.

```bash
dev-decisions triage-issues [owner/repo] --limit 20
dev-decisions triage-issues --state all --dry-run
```

Classifications: `kind` (bug/feature/docs/question/chore) + `priority` (low/medium/high/critical).

## Changelog generation

`changelog` collects commits since a ref, classifies each, and groups into Keep-a-Changelog markdown.

```bash
dev-decisions changelog --since v0.1.0
dev-decisions changelog --since v0.1.0 --write
```

Sections: Added, Changed, Fixed, Removed, Security.

## ZCode agent routing

`zcode-gate` reads JSON from stdin (PreToolUse hook). It:

1. Fast-exits on non-Bash commands.
2. Detects destructive patterns (`git push --force`, `rm -rf`, `DROP TABLE`, etc.).
3. Runs the `safety` task (local GLiNER) to check reversibility.
4. With `advisory_only = true` (default): warns but proceeds (exit 1).
5. With `advisory_only = false`: blocks (exit 2).
6. On `git commit`/`git push`: runs `scan-staged` / `classify-diff` and returns their exit code.

```json
{
  "tool_name": "Bash",
  "tool_input": { "command": "git push --force origin main" }
}
```

Config in `~/.config/dev-decisions/config.toml`:

```toml
[gate]
advisory_only = true   # true = warn+proceed; false = block
```

Enable in `~/.zcode/cli/config.json`:

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PreToolUse": [
        {
          "matcher": "Bash",
          "hooks": [
            {
              "type": "command",
              "command": "~/.local/bin/dev-decisions zcode-gate",
              "timeout": 10
            }
          ]
        }
      ]
    }
  }
}
```

## Safety rules

1. **Secrets always block** (local, deterministic, no vendor call).
2. **PII warns only** — never blocks; `git commit --no-verify` documented in output.
3. **Vendor guard**: local scan runs first; any secret hit → vendor call skipped (don't leak what we're scanning for).
4. **Sensitive repos**: `.dev-decisions.toml` with `sensitive = true` → vendor calls denied unless `--allow-vendor` is passed.
5. **Credentials**: env vars only (`FASTINO_API_KEY`, `TYPESAFE_API_KEY`). Never in source, args, logs, or output.
6. **Max diff chars**: default 12k; configurable per repo.
7. **Destructive commands**: regex-detected; `safety` task checks reversibility; `advisory_only` controls warn vs block.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Pass |
| 1 | Warn (advisory, human review recommended) |
| 2 | Block |
| 3 | Error |

Matches git hook convention: `0` = proceed, non-zero = stop.

## Config precedence

Defaults ← `~/.config/dev-decisions/config.toml` ← `.dev-decisions.toml` (repo) ← `DEV_DECISIONS_*` env vars.

Key flags:
- `block_on_classification`: false in v1 (advisory); flip once you have calibration data.
- `confidence_floor`: 0.7 by default.
- `max_diff_chars`: 12000 by default.
- `provider`: `auto` (default; sys1 router) | `decide` | `jev` | `glide` | `drex` | `local` | `both`.
- `local_venv`: path to the gliner2 venv (default `/private/tmp/gliner-decide`).
- `local_model`: Hugging Face model id (default `fastino/GLiNER2.5-Decide`).
- `gate.advisory_only`: true = warn+proceed; false = block.

## Dispositions (HITL record)

Gates are advisorial, but a negative verdict must end in a **recorded human decision** — that is what makes the advice layer load-bearing. Ignoring a flag silently is the failure mode this exists to prevent.

```bash
dev-decisions disposition <gate> <target> --status fixed
dev-decisions disposition <gate> <target> --status waived --reason "criterion deferred to plan X"
dev-decisions disposition <gate> <target> --status overridden --reason "gate misread the diff; the call is guarded upstream"
```

- `fixed` — the flag was right; the item was reworked.
- `waived` — proceeding despite the flag; **reason required**.
- `overridden` — the gate was wrong (model error); **reason required**.

Policy (2026-10-02): negatives **gate status transitions** — a plan with undispositioned gate flags stays `draft`, a close-out with NOT SUPPORTED evidence stays `active`. Waive/override reasons are the calibration signal: a gate with a recurring override pattern needs rework, and a stable low override rate is what earns the flip from advisorial to blocking (per gate, per the `block_on_classification` / `advisory_only` flags).

**Safety exception:** destructive AND irreversible commands hard-block in `zcode-gate` regardless of `advisory_only` — no disposition waives that in-band; rerun without the destructive form.

## JSONL schema

```json
{
  "ts": "2026-09-26T...",
  "op": "classify-diff",
  "repo": "my-project",
  "trigger": "git-pre-push",
  "provider": "both",
  "providers_used": ["decide", "jev"],
  "input_chars": 1234,
  "input_sha256": "abc123...",
  "heads": { "decide": {...}, "jev": {...} },
  "verdict": "escalated",
  "escalated": true,
  "latency_ms": 1234,
  "task": "deps_risk",
  "labels": ["bug", "feature"],
  "destructive": true,
  "reversible": "no",
  "advisory_only": true
}
```

New fields in v0.2.0: `task`, `labels`, `destructive`, `reversible`, `advisory_only`.

## Calibration loop

After a few weeks, the JSONL log contains (input, model, confidence, human-outcome) pairs. Fit real thresholds from it:
1. Export log: `dev-decisions log --format json > calibration.jsonl`
2. For each provider × head, plot confidence vs human-accepted rate.
3. Set per-provider per-head floor from the curve (the JEV-as-a-Judge paper: thresholds don't transfer — fit locally).

## Requirements

- Python 3.10+ (stdlib-only core)
- git
- `gh` CLI (for PR gating and issue triage)
- For hosted providers: `FASTINO_API_KEY` and/or `TYPESAFE_API_KEY` env vars (`LIQUID_API_KEY` for the d1 backup; Drex's key lives in the second-brain project config)
- For local provider: `gliner2` installed in a compatible venv

## License

Apache-2.0
