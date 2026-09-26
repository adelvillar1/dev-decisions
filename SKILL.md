---
name: dev-decisions
description: Use decision models (Jev, GLiNER-2.5-Decide) to classify diffs, scan commits for secrets/PII, and gate git operations in dev workflows. Use when the user mentions dev-decisions, decision-model gates, pre-commit scans, diff classification, Jev for code review, GLiNER for secrets, decision-model workflows, or decision logging. Triggers on: "scan the staged diff", "classify this commit", "what type of change is this", "install dev-decisions hooks", "decision log", "calibration data for thresholds".
---

# dev-decisions: decision-model gates for git + ZCode workflows

A stdlib-only Python CLI (`dev-decisions`) that uses hosted decision models to classify diffs, scan for secrets/PII, and log every decision to JSONL as a calibration set for fitting thresholds later.

## Quick start

```bash
# Fleet management
dev-decisions status                    # all repos under ~/Projects
dev-decisions bulk-install              # install hooks in every repo
dev-decisions bulk-install --force      # overwrite existing hooks
dev-decisions status --root ~/code      # scan a different root
```

## Architecture

Three layers, each independently useful:

| Layer | What it does | When it runs |
|---|---|---|
| **git hooks** | pre-commit → `scan-staged` (block secrets); pre-push → `classify-diff` | Automatic, per-repo |
| **ZCode hook** | `PreToolUse` on Bash → blocks agent-run `git commit`/`git push` if scan/classify fails | Automatic, global (opt-in) |
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
| Agreement / confidence gating | **both` / `local+decide` / `local+jev` | Capture disagreements for calibration |

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

## Safety rules

1. **Secrets always block** (local, deterministic, no vendor call).
2. **PII warns only** — never blocks; `git commit --no-verify` documented in output.
3. **Vendor guard**: local scan runs first; any secret hit → vendor call skipped (don't leak what we're scanning for).
4. **Sensitive repos**: `.dev-decisions.toml` with `sensitive = true` → vendor calls denied unless `--allow-vendor` is passed.
5. **Credentials**: env vars only (`FASTINO_API_KEY`, `TYPESAFE_API_KEY`). Never in source, args, logs, or output.
6. **Max diff chars**: default 12k; configurable per repo.

## Config precedence

Defaults ← `~/.config/dev-decisions/config.toml` ← `.dev-decisions.toml` (repo) ← `DEV_DECISIONS_*` env vars.

Key flags:
- `block_on_classification`: false in v1 (advisory); flip once you have calibration data.
- `confidence_floor`: 0.7 by default.
- `max_diff_chars`: 12000 by default.
- `provider`: `decide` | `jev` | `local` | `both`.
- `local_venv`: path to the gliner2 venv (default `/private/tmp/gliner-decide`).
- `local_model`: Hugging Face model id (default `fastino/GLiNER2.5-Decide`).

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
  "latency_ms": 1234
}
```

## Calibration loop

After a few weeks, the JSONL log contains (input, model, confidence, human-outcome) pairs. Fit real thresholds from it:
1. Export log: `dev-decisions log --format json > calibration.jsonl`
2. For each provider × head, plot confidence vs human-accepted rate.
3. Set per-provider per-head floor from the curve (the JEV-as-a-Judge paper: thresholds don't transfer — fit locally).

## ZCode hook

Enable in `~/.zcode/cli/config.json` (backup first):

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

The hook no-ops on non-git Bash commands (regex early-exit ≈ instant).

## Phase 2 (not in this build)

- `install-local`: uv 3.12 venv + `gliner2[local]` PII model for offline span-level scan
- `triage-issues`: batch issue classification
- `calibrate`: fit thresholds from JSONL
- Adapter routing: per-domain adapter on one loaded GLiNER2 base

## Gotchas

- **Python 3.14**: core CLI is stdlib-only and runs fine; local GLiNER needs ≤3.12 (Phase 2 pins uv Python 3.12).
- **Decide declines**: `risk: null` is common on short diffs — treat as "not enough signal", not an error.
- **Jev endpoint**: `POST https://api.typesafe.ai/v1/systemone` with `{ state, questions, model }` — NOT the OpenAI chat shape.
- **TTESS keys**: two TTESS scripts (`scripts/gather-hosted-shadow.py`, `scripts/eval-hosted-shadow.py`) have hardcoded Fastino keys — rotate them and use env vars.
- **Jev costs**: $0.042/M input tokens; output is free. The 313-token test call costs ~$0.000013.
