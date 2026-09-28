---
name: dev-decisions
description: Use decision models (Jev, GLiNER-2.5-Decide) to classify diffs, scan commits for secrets/PII, gate git operations, PRs, and issues in dev workflows. Use when the user mentions dev-decisions, decision-model gates, pre-commit scans, diff classification, Jev for code review, GLiNER for secrets, decision-model workflows, decision logging, PR gating, issue triage, changelog generation, or ZCode agent routing. Triggers on: "scan the staged diff", "classify this commit", "what type of change is this", "install dev-decisions hooks", "decision log", "calibration data for thresholds", "PR gate", "triage issues", "generate changelog", "ZCode gate".
---

# dev-decisions: decision-model gates for git + ZCode workflows

A stdlib-only Python CLI (`dev-decisions`) that uses hosted decision models to classify diffs, scan for secrets/PII, gate PRs/issues, generate changelogs, and log every decision to JSONL as a calibration set for fitting thresholds later.

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
3. Nothing else. The hook classifies every push through sys1's `decide` provider; without keys it degrades to warn-and-proceed (fail-open by design); sensitive repos skip vendor calls unless `--allow-vendor`.

Use this when onboarding a new machine (keys file + `bulk-install`), after cloning a batch of repos, or when adding a project to your `~/Projects` directory.

## Provider routing

**Active-provider policy (2026-09-28): hosted Fastino (`decide`) and TypeSafe Jev (`jev`) are the only active providers.** Local GLiNER is supported by design but not encouraged (its wire drops head instructions; nothing critical relies on it). Additional hosted APIs are explored before inclusion.

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
```

Labels applied: `bug`, `feature`, `refactor`, `docs`, `chore`, `test`, `ci` (multi-label).

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
- `provider`: `decide` | `jev` | `local` | `both`.
- `local_venv`: path to the gliner2 venv (default `/private/tmp/gliner-decide`).
- `local_model`: Hugging Face model id (default `fastino/GLiNER2.5-Decide`).
- `gate.advisory_only`: true = warn+proceed; false = block.

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
- For hosted providers: `FASTINO_API_KEY` and/or `TYPESAFE_API_KEY` env vars
- For local provider: `gliner2` installed in a compatible venv

## License

Apache-2.0
