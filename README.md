# dev-decisions

Decision-model gates for git + ZCode workflows. Scans secrets/PII from commits, classifies diffs using multiple providers, and logs every decision to JSONL for calibration.

**v0.2.0** — PR gating, issue triage, changelog generation, ZCode agent routing with destructive-command detection.

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
dev-decisions scan-staged --deep   # PII span model (fully local)

# Classify a diff (choose provider)
dev-decisions classify-diff --provider decide
dev-decisions classify-diff --provider jev
dev-decisions classify-diff --provider local
dev-decisions classify-diff --task deps_risk   # explicit task

# PR gating (local-first)
dev-decisions pr-gate [branch] --dry-run
dev-decisions pr-gate 123 --provider local

# Issue triage (dry-run by default)
dev-decisions triage-issues [owner/repo] --limit 20
dev-decisions triage-issues --state all --dry-run

# Changelog generation
dev-decisions changelog --since v0.1.0
dev-decisions changelog --since v0.1.0 --write

# Fleet scan for API drift
dev-decisions fleet-scan --root ~/Projects --provider local

# View decision log (your calibration dataset)
dev-decisions log --tail 20
dev-decisions log --format json > calibration.jsonl

# Remove hooks
dev-decisions remove-hooks /path/to/repo
```

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
