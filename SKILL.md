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

Use this when onboarding a new machine, after cloning a batch of repos, or when adding a project to your `~/Projects` directory.

## Provider routing

| Job | Model | Why |
|---|---|---|
| Diff classification, offline / no API cost | **GLiNER2 local** (`/private/tmp/gliner-decide`) | Free, private, zero-latency for small diffs |
| Diff classification, zero infra | **Decide** (`fastino/GLiNER-2.5-Decide`) | Fast, declines on ambiguity |
| Calibrated judgments, multi-question | **Jev** (`jev-1.13.0`) | Choice/Score/Noul, published training method |
| Agreement / confidence gating | **both** / `local+decide` / `local+jev` | Capture disagreements for calibration |

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

Both providers return confidence. The JEV-as-a-Judge paper's finding applies: **confidence is an escalation signal, not a certificate**. Default floor is 0.7; null/declined verdicts always escalate.

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
