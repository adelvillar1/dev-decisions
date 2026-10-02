# dev-decisions

Decision-model gates for git + ZCode workflows. Scans secrets/PII from commits, classifies diffs using multiple providers, and logs every decision to JSONL for calibration.

**v0.3.0** — provider telemetry, live dashboard, feedback loop for calibration, ModernBERT eval provider.

## Architecture

Three layers, each independently useful:

| Layer | What it does | When it runs |
|---|---|---|
| **git hooks** | `pre-commit` → `scan-staged` (block secrets); `pre-push` → `classify-diff` | Automatic, per-repo |
| **ZCode hook** | `PreToolUse` on Bash → blocks agent-run `git commit`/`git push` if scan/classify fails | Automatic, global (opt-in) |
| **CLI** | All subcommands, invocable from ZCode skill or terminal | On-demand |

Shared JSONL log at `~/.local/share/dev-decisions/logs/YYYY/MM/DD/events.jsonl` is the calibration dataset.

![dev-decisions architecture](docs/architecture.svg)

## Providers

| Job | Model | Why |
|---|---|---|
| Diff classification (default) | **Decide — hosted Fastino API** (`decide`) | The active fastino provider; fast, declines on ambiguity |
| Diff classification (offline) | **GLiNER2.5-Decide** (`local`) | Same Decide model offline via sys1 — supported by design, **not encouraged** |
| Calibrated judgments, multi-question | **Jev** (`jev-1.13.0`) | Choice/Score/Noul, published training method |
| Eval-only raw inference for calibration | **ModernBERT** (`answerdotai/ModernBERT-base`) | Sentence encoder, no fine-tuning, logs raw predictions |

### Candidate models (opt-in, via sys1 flags)

`--provider` accepts every provider `sys1` registers, including candidate
models that are wired but disabled by default: **CLM-8B** (`clm`),
**openjev/JevK5** (`openjev`), **Kev** (`kev`), **Tev1** (`tev1`),
**Laya** (`laya`), and the GLiNER variants **`decide_1b`** /
**`decide_multi`**. Turn one on in `~/.config/sys1/config.toml`
(`providers.enabled = "core,clm"`) or flip it live in the sys1 control
dashboard (`GET /dashboard` on the sys1 service, port 8400), then point its
`{id}_api_url` / `{id}_model` / key env at your endpoint. Use `all`, `core`,
`optin`, or `candidates` as fan-out tokens. `dev-decisions providers`-style
inspection lives in the sys1 CLI: `sys1 providers --all`, `sys1 doctor`.

## API keys for git hooks

Git hooks don't inherit your shell environment. `install-hooks` wires every
hook it manages to source an optional key file:

```bash
# ~/.config/dev-decisions/env  (chmod 600; one KEY=value per line)
FASTINO_API_KEY=...
TYPESAFE_API_KEY=...
```

With `FASTINO_API_KEY` present, `pre-push` (classify-diff) reaches the
hosted Fastino API; without it the hook warns and proceeds (fail-open by
design). `pre-commit` (scan-staged) is fully local and needs no keys.

## Local GLiNER2.5-Decide setup (supported by design, not encouraged)

The `local` provider runs `fastino/GLiNER2.5-Decide` through
`sys1` using the fastino-prescribed classification API (one decode for all
heads, full probabilities), in a standalone uv venv (default
`/private/tmp/gliner-decide`) — fully offline, free, and safe for sensitive
repos. `/private/tmp` is wiped on reboot; move it elsewhere via
`providers.local_venv` if you want it to survive. For a load-once server
(recommended: ~10s model load per call → ~150ms steady-state), see
`service/classifier_server.py` in the sys1 repo and set
`providers.local_server_url`.

```bash
# 1. Create the venv (Python ≤ 3.12 — GLiNER2 needs torch that 3.13/3.14 lack)
uv venv --python 3.12 /private/tmp/gliner-decide

# 2. Install gliner2 + CPU torch into it
uv pip install --python /private/tmp/gliner-decide/bin/python "gliner2[local]"
uv pip install --python /private/tmp/gliner-decide/bin/python torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python /private/tmp/gliner-decide/bin/python protobuf

# 3. Models download from Hugging Face on first use and are cached:
#    fastino/GLiNER2.5-Decide                  (classification)
#    fastino/gliner2-privacy-filter-PII-multi  (scan-staged --deep span extraction)
```

Point the CLI at a different venv or model in config:

```toml
[providers]
local_venv = "/private/tmp/gliner-decide"
local_model = "fastino/GLiNER2.5-Decide"
modernbert_model = "answerdotai/ModernBERT-base"
```

## ModernBERT eval provider

ModernBERT runs as a **sentence encoder** inside the same uv venv. It encodes the diff once, then scores each candidate label by cosine similarity against the label's standalone embedding. No fine-tuning — this is calibration data collection.

```bash
# Run ModernBERT on a diff
dev-decisions classify-diff --provider modernbert --task change

# Fleet scan with ModernBERT
dev-decisions fleet-scan --provider modernbert
```

Results are logged with provider tag `modernbert_raw` so they can be filtered from production metrics. The inline script captures `all_scores`, `top2_gap`, and `embedding_norm` per head — these are the signals that tell you what fine-tuning would need to improve.

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
| `plan_surface_map` / `plan_deps` | `plan-surface` command | Criterion->module map / pairwise criterion order |

![v0.2.0 workflows](docs/workflows.svg)

## Plan reconcile + calibration

`plan-reconcile <plan.md>` grades a plan's surface artifact against its actual commits (tau, edge precision, feedback rows). `calibration` reports per-head graded-row counts, accuracy curves, and floor-fit readiness across both feedback stores. See SKILL.md for detail.

## Plan surface

`plan-surface` is the feed-forward counterpart of `plan-gate`: it precomputes the judgment graph a plan decomposer should assemble against — criterion-to-module mapping, pairwise criterion dependencies with confidences, thresholded DAG with topological order, uncertain band, risk flags — and writes the artifact to `~/.local/share/dev-decisions/surfaces/`. See SKILL.md ("Plan surface") for the layer detail and calibration notes.

```bash
dev-decisions plan-surface docs/plans/2026-10-02-feature.md --repo-root .
```

## Telemetry and dashboard

Every provider call now emits structured telemetry: latency, error kind, token usage (Decide), noul/null rates (Jev/local), top2-gap and embedding norm (ModernBERT). All signals flow into the JSONL log.

```bash
# Start the live local dashboard
dev-decisions dashboard --port 8765

# Open http://localhost:8765 to see:
#   - provider health (calls, errors, latency p50/p95 + latency distribution histogram)
#   - confidence histograms per provider (coarse 6-bin + fine 20-bin spread)
#   - ModernBERT signals (top2-gap spread + embedding-norm spread histograms)
#   - cross-provider agreement matrix
#   - calibration curves (once you add feedback)
```

## Feedback loop (calibration ground truth)

The dashboard shows confidence distributions, but the only way to know if a provider is *correct* is to record human feedback:

```bash
# After a classify-diff run, find the input_sha256 in the log:
dev-decisions log --format json | jq -r '.[-1].input_sha256'

# Record the correct label:
dev-decisions feedback <sha> --task change --label fix --provider modernbert_raw --note "clear typo fix"
```

The dashboard joins feedback with events on `(input_sha256, task)` to compute confidence-vs-correctness curves per provider. This is the signal that drives fine-tuning decisions: if ModernBERT's high-confidence predictions are wrong more often than Decide's, the encoder needs training.

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

# Classify a diff (default: local fastino GLiNER2.5-Decide via sys1)
dev-decisions classify-diff
dev-decisions classify-diff --provider decide   # hosted fallback (FASTINO_API_KEY)
dev-decisions classify-diff --provider jev
dev-decisions classify-diff --task deps_risk   # explicit task
dev-decisions classify-diff --allow-vendor     # one-off vendor call on a sensitive repo

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

# Record human feedback for calibration
dev-decisions feedback <sha> --task change --label fix --provider modernbert_raw

# Start the live telemetry dashboard
dev-decisions dashboard --port 8765

# Show the effective merged config (defaults ← global ← repo ← env)
dev-decisions config

# Environment check: python, git, keys, config, hooks, log dir
dev-decisions doctor

# Remove hooks
dev-decisions remove-hooks /path/to/repo
```

## How a decision flows

`classify-diff`, `scan-staged`, `pr-gate`, and `fleet-scan` share one pipeline: local secret scan first, then guards, then the task registry, then provider routing, then the confidence gate. Every step is appended to the JSONL log.

![decision flow](docs/decision-flow.svg)

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

## Config reference

This is the full set of knobs (mirrors [`config.example.toml`](config.example.toml)):

```toml
[scan]
block_on_secret = true        # exit 2 on a secret hit
warn_on_pii = true            # exit 1 on a PII hit (never blocks)
max_diff_chars = 12000        # truncate diffs before scanning/classifying

[classify]
provider = "decide"           # decide | jev | local | both
block_on_classification = false
confidence_floor = 0.7
escalate_on_null = true
allow_vendor_on_sensitive = false

[providers]
decide_api_url = "https://api.fastino.ai/v1/chat/completions"
decide_model = "fastino/GLiNER-2.5-Decide"
jev_api_url = "https://api.typesafe.ai/v1/systemone"   # NOT the chat shape
jev_model = "jev-1.13.0"
local_venv = "/private/tmp/gliner-decide"
local_model = "fastino/GLiNER2.5-Decide"
request_timeout_seconds = 30
max_retries = 2
retry_backoff_seconds = 5

[hooks]
pre_commit = "scan-staged"
pre_push = "classify-diff"

[gate]
advisory_only = true          # true = warn+proceed (exit 1); false = block (exit 2)
```

Repo-local `.dev-decisions.toml` supports one extra flag: `[repo] sensitive = true` denies all vendor calls for that repo.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `FASTINO_API_KEY not set — skipping Decide` | `export FASTINO_API_KEY=…`, or run with `--provider local` (no key needed) |
| `Local GLiNER venv not found at …` | Create it (see [Local GLiNER setup](#local-gliner-setup)) or point `providers.local_venv` at an existing one. `/private/tmp` is wiped on reboot |
| `error: gh CLI required` | `brew install gh && gh auth login` |
| `Repo is marked sensitive — vendor classification skipped` | Intended. Pass `--allow-vendor` for one call, or set `allow_vendor_on_sensitive = true` |
| Decide returns `null` on a head | Not an error — the model declined (usually a short/ambiguous diff). Treat as low signal; nulls escalate |
| Jev calls fail or return unexpected shapes | Jev uses `POST /v1/systemone` with `{state, questions, model}` — not the OpenAI chat shape. Check `providers.jev_api_url` |
| Commits feel slow on huge diffs | Lower `scan.max_diff_chars` (e.g. 6000); the local regex scan is fast, vendor calls scale with size |
| Python 3.13/3.14 + local provider | The stdlib CLI runs on any python3 ≥ 3.10, but GLiNER2's torch needs the 3.12 venv (see setup above) |

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
  "advisory_only": true,
  "telemetry": {
    "decide": {
      "latency_ms": 820,
      "http_status": 200,
      "retries": 0,
      "token_prompt": 512,
      "token_completion": 128,
      "token_total": 640,
      "structured_ok": true
    },
    "modernbert_raw": {
      "latency_ms": 7800,
      "error_kind": null,
      "top2_gap": 0.12,
      "embedding_norm": 1.0
    }
  }
}
```

New fields in v0.2.0: `task`, `labels`, `destructive`, `reversible`, `advisory_only`.

New fields in v0.3.0: `telemetry` (per-provider dict with latency, error_kind, token usage, structured_ok, top2_gap, embedding_norm, null_label_count, noul_count). Feedback records go to `logs/feedback/feedback.jsonl` and are joined on `input_sha256` + `task` for calibration curves.

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
