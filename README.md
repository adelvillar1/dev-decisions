# dev-decisions

Decision-model gates for git + ZCode workflows. Scans secrets/PII from commits, classifies diffs using multiple providers, and logs every decision to JSONL for calibration.

## Architecture

Three layers, each independently useful:

| Layer | What it does | When it runs |
|---|---|---|
| **git hooks** | `pre-commit` → `scan-staged` (block secrets); `pre-push` → `classify-diff` | Automatic, per-repo |
| **ZCode hook** | `PreToolUse` on Bash → blocks agent-run `git commit`/`git push` if scan/classify fails | Automatic, global (opt-in) |
| **CLI** | All subcommands, invocable from ZCode skill or terminal | On-demand |

Shared JSONL log at `~/.local/share/dev-decisions/logs/YYYY/MM/DD.jsonl` is the calibration dataset.

## Providers

| Job | Model | Why |
|---|---|---|
| Diff classification, offline / no API cost | **GLiNER2 local** | Free, private, zero-latency |
| Diff classification, zero infra | **Decide** (`fastino/GLiNER-2.5-Decide`) | Fast, declines on ambiguity |
| Calibrated judgments, multi-question | **Jev** (`jev-1.13.0`) | Choice/Score/Noul, published training method |

## Install

```bash
# Clone
git clone https://github.com/adelvillar1/dev-decisions.git
cd dev-decisions

# Install CLI
mkdir -p ~/.local/bin
cp dev-decisions ~/.local/bin/dev-decisions
chmod +x ~/.local/bin/dev-decisions

# Install config (optional)
mkdir -p ~/.config/dev-decisions
cp config.example.toml ~/.config/dev-decisions/config.toml

# Install hooks in a repo
dev-decisions install-hooks /path/to/repo

# Fleet install: all repos under ~/Projects
dev-decisions bulk-install

# Check environment
dev-decisions doctor
```

## Usage

```bash
# Fleet status across ~/Projects
dev-decisions status

# Scan staged diff for secrets/PII
dev-decisions scan-staged

# Classify a diff (choose provider)
dev-decisions classify-diff --provider decide
dev-decisions classify-diff --provider jev
dev-decisions classify-diff --provider local

# View decision log (your calibration dataset)
dev-decisions log --tail 20
dev-decisions log --format json > calibration.jsonl

# Remove hooks
dev-decisions remove-hooks /path/to/repo
```

## Safety rules

1. **Secrets always block** (local, deterministic, no vendor call).
2. **PII warns only** — never blocks; `git commit --no-verify` documented in output.
3. **Vendor guard**: local scan runs first; any secret hit → vendor call skipped (don't leak what we're scanning for).
4. **Sensitive repos**: `.dev-decisions.toml` with `sensitive = true` → vendor calls denied unless `--allow-vendor` is passed.
5. **Credentials**: env vars only (`FASTINO_API_KEY`, `TYPESAFE_API_KEY`). Never in source, args, logs, or output.
6. **Max diff chars**: default 12k; configurable per repo.

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

## JSONL log schema

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

After a few weeks, the JSONL log contains (input, model, confidence, human-outcome) pairs:

1. Export log: `dev-decisions log --format json > calibration.jsonl`
2. For each provider × head, plot confidence vs human-accepted rate.
3. Set per-provider per-head floor from the curve (thresholds don't transfer — fit locally).

## Requirements

- Python 3.10+ (stdlib-only core)
- git
- For hosted providers: `FASTINO_API_KEY` and/or `TYPESAFE_API_KEY` env vars
- For local provider: `gliner2` installed in a compatible venv

## License

Apache-2.0
