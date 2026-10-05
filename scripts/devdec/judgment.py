"""decision-model calls: sys1 chains, fanout, consistency, providers, task registry."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .gitops import diff_content_text

# section: judgment (moved verbatim from dev_decisions.py)

# ── sys1 bootstrap (optional dependency) ─────────────────────────────────────
# dev-decisions is a single-file stdlib script. When the `sys1` package is
# importable, classify/parse/gate calls delegate to it so the two stay in
# lockstep (one canonical provider implementation, one canonical wire shape).
# When it isn't, the inline providers below keep the tool fully self-contained.
#
# Search order:
#   1. standard Python path (pip install -e / editable / site-packages)
#   2. $DEV_DECISIONS_SYS1_PATH (a directory that contains the `sys1` package)
#   3. ~/Projects/sys1/src (the canonical local checkout)
#   4. ../sys1/src relative to this script (sibling checkout)
#   5. installed sys1 package's parent directory (already in sys.path)
def _bootstrap_sys1():
    try:
        import sys1  # type: ignore[import-not-found]
        return sys1
    except ImportError:
        pass
    candidates: list[Path] = []
    env = os.environ.get("DEV_DECISIONS_SYS1_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "Projects" / "sys1" / "src")
    here = Path(__file__).resolve()
    for parent in here.parents[:4]:
        candidates.append(parent / "sys1" / "src")
    for cand in candidates:
        s = str(cand)
        if not cand.is_dir():
            continue
        if s not in sys.path:
            sys.path.insert(0, s)
        try:
            import sys1  # type: ignore[import-not-found]
            return sys1
        except ImportError:
            # remove and continue — keep sys.path clean on miss
            try:
                sys.path.remove(s)
            except ValueError:
                pass
            continue
    return None


sys1 = _bootstrap_sys1()
try:
    import sys1.plansurface as _plansurface  # type: ignore[import-not-found]  # noqa: E402
except Exception:  # sys1 too old for the plansurface module
    _plansurface = None

# --provider choices: dynamic from the sys1 registry, so candidate models
# (clm, kev, tev1, decide_1b, ...) show up as soon as sys1 registers them —
# plus the fan-out aliases. Falls back to the four core ids when sys1 isn't
# installed (self-contained mode).
_FALLBACK_PROVIDER_IDS = ["decide", "jev", "local", "modernbert"]
_PROVIDER_ALIASES = ["both", "all", "core", "optin", "candidates", "auto"]


def _provider_choices() -> list[str]:
    if sys1 is None:
        return _FALLBACK_PROVIDER_IDS + ["both"]
    try:
        return sorted(sys1.REGISTRY) + _PROVIDER_ALIASES
    except Exception:
        return _FALLBACK_PROVIDER_IDS + ["both"]


PROVIDER_CHOICES = _provider_choices()


def _sys1_chain(provider: str) -> list[str]:
    """Map dev-decisions' provider string to a sys1 provider-chain list.

    The `both` alias here includes local GLiNER because dev-decisions has always
    treated it as part of the `both` fan-out (sys1's own `both` is decide+jev;
    this is the dev-decisions-specific interpretation). Set tokens ("all",
    "core", "optin", "candidates") delegate to sys1.resolve_providers so they
    track the registry; a single id passes through (sys1's flag gate and
    availability checks decide whether it actually runs).
    """
    if provider == "both":
        return ["decide", "jev", "local"]
    if provider in ("all", "core", "optin", "candidates") and sys1 is not None:
        try:
            resolved = sys1.resolve_providers(provider)
            if resolved:
                return resolved
        except Exception:
            pass
    if provider == "all":
        return ["decide", "jev", "local", "modernbert"]
    if "+" in provider:
        return [p for p in provider.split("+") if p]
    return [provider]


# sys1 registers ModernBERT under id "modernbert"; dev-decisions has always
# logged it as "modernbert_raw" so the dashboard can filter it from production
# metrics. The remap keeps both vocabularies intact.
_SYS1_PROVIDER_REMAP = {"modernbert": "modernbert_raw"}


def _sys1_classify(
    provider: str,
    task: str,
    text: str,
    cfg: dict,
) -> dict:
    """
    Delegate a classify call to sys1 and reshape the result into the
    dev-decisions CLI vocabulary:
      - provider IDs remapped (modernbert → modernbert_raw)
      - the same `results / providers_used / telemetry / escalated / summary`
        dict shape the inline code path produces.
    Raises if sys1 isn't importable; callers should fall back to the inline path.
    """
    if sys1 is None:
        raise RuntimeError("sys1 not available")
    chain = _sys1_chain(provider)
    result = sys1.classify(chain, task, text, cfg=cfg, log=False)
    remap = _SYS1_PROVIDER_REMAP
    answers = {remap.get(p, p): a for p, a in result.answers.items()}
    telemetry = {remap.get(p, p): t for p, t in result.telemetry.items()}
    providers_used = [remap.get(p, p) for p in result.providers_used]
    return {
        "results": answers,
        "providers_used": providers_used,
        "telemetry": telemetry,
        "escalated": result.escalated,
        "summary": result.summary,
        "latency_ms": result.latency_ms,
        "verdict": result.verdict,
    }


def _sys1_classify_single(provider: str, task: str, text: str, cfg: dict) -> dict:
    """
    sys1-backed replacement for the per-provider blocks in cmd_pr_gate /
    cmd_triage_issues / cmd_changelog, which expect a single `parsed` dict
    keyed by head id. Returns answers for the first clearing provider, or {}
    if none answered. Falls back to {} so callers can keep their existing
    "extract labels from parsed" loop.
    """
    if sys1 is None:
        raise RuntimeError("sys1 not available")
    # The single-provider commands historically took the base provider (e.g.
    # "decide" or "jev") and used that one — but `both` used decide+jev+local.
    # We preserve the original semantics by running the same chain and using
    # the first provider whose answers are non-empty.
    if provider == "auto":
        resolved_task = sys1.get_task(task) if isinstance(task, str) else task
        chain, _route_reason = sys1.routing.route_decision(resolved_task, text, cfg)
        if not chain:
            chain = _sys1_chain("decide")
    else:
        chain = _sys1_chain(provider)
    result = sys1.classify(chain, task, text, cfg=cfg, log=False)
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        answers = result.answers.get(mapped)
        if answers:
            return {"provider": mapped, "answers": answers, "telemetry": result.telemetry.get(mapped, {})}
    return {"provider": chain[0] if chain else "", "answers": {}, "telemetry": {}}


_FANOUT_MAX_FILES = 12


def _split_diff_files(diff: str, max_files: int = _FANOUT_MAX_FILES) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Split a unified diff into (path, chunk) pairs, largest chunks first, capped
    at max_files. Chunks keep their 'diff --git' header so each section is a
    well-formed per-file diff. Returns (included, omitted_paths).
    """
    chunks: list[tuple[str, str]] = []
    cur_path: str | None = None
    cur: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur_path is not None:
                chunks.append((cur_path, "".join(cur)))
            m = re.search(r" b/(.+)$", line.rstrip("\n"))
            cur_path = m.group(1) if m else line[len("diff --git "):].strip()
            cur = [line]
        elif cur_path is not None:
            cur.append(line)
    if cur_path is not None:
        chunks.append((cur_path, "".join(cur)))
    chunks.sort(key=lambda pc: len(pc[1]), reverse=True)
    return chunks[:max_files], [p for p, _ in chunks[max_files:]]


def _sys1_classify_fanout(provider: str, diff: str, cfg: dict) -> dict:
    """
    Speculative fan-out port (pattern from browser-use/jev-ultrafast): per-file
    heads (risky noul + action choice per file) AND the standard pr_gate heads
    all ride in ONE /v1/systemone request. Questions run in parallel, so
    per-file coverage costs ~nothing in latency over the 3-head call; the app
    uses the heads it needs and discards the rest. Built as a dynamic Task so
    the built-in registry and per-provider head builders are untouched.
    """
    if sys1 is None:
        raise RuntimeError("sys1 not available")
    included, omitted = _split_diff_files(diff)
    heads: list = []
    for h in _build_pr_gate_heads()["jev"]:
        heads.append(sys1.make_choice(h["instructions"], list(h["criteria"].keys()), id=h["id"], descriptions=dict(h["criteria"])))
    file_meta: list[dict] = []
    for i, (path, chunk) in enumerate(included):
        heads.append(sys1.make_noul(
            f"Does the change to {path} touch risky paths (auth, authz, payments, data deletion, secrets, migrations, concurrency)?",
            id=f"f{i}_risky",
        ))
        heads.append(sys1.make_choice(
            f"What should happen for {path} based on its diff?",
            ["approve", "comment", "block"],
            id=f"f{i}_action",
            descriptions={
                "approve": "No action needed for this file.",
                "comment": "Worth a review comment.",
                "block": "Should block the PR until fixed.",
            },
        ))
        file_meta.append({"index": i, "path": path, "chunk": chunk})
    parts = [
        f"PR diff with {len(file_meta) + len(omitted)} changed files. "
        f"Heads f0_risky/f0_action .. f{len(file_meta) - 1}_risky/f{len(file_meta) - 1}_action "
        "map to the numbered file sections below, in order."
    ]
    if omitted:
        parts.append("Files beyond the per-file limit have no heads; they are listed here for context only: " + ", ".join(omitted))
    for m in file_meta:
        parts.append(f"=== FILE f{m['index']}: {m['path']} ===\n{m['chunk']}")
    state = "\n\n".join(parts)[: cfg["scan"]["max_diff_chars"]]
    task = sys1.types.Task(
        id="pr_gate_fanout",
        heads=heads,
        description="PR gate with speculative per-file fan-out heads",
    )
    chain = _sys1_chain(provider) if provider != "auto" else sys1.routing.route_decision(task, state, cfg)[0] or _sys1_chain("decide")
    result = sys1.classify(chain, task, state, cfg=cfg, log=False)
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        answers = result.answers.get(mapped)
        if answers:
            return {
                "provider": mapped,
                "answers": answers,
                "telemetry": result.telemetry.get(mapped, {}),
                "files": [m["path"] for m in file_meta],
                "omitted": omitted,
                "latency_ms": result.latency_ms,
            }
    return {
        "provider": chain[0] if chain else "",
        "answers": {},
        "telemetry": {},
        "files": [m["path"] for m in file_meta],
        "omitted": omitted,
        "latency_ms": result.latency_ms,
    }


def _merge_draws(draws: list) -> dict:
    """
    Majority-vote merge of n per-provider answer dicts (dynamic tasks,
    plan-gate self-consistency). Noul heads: mean p across draws, stdev, and
    an unstable flag when draws straddle the 0.5 cut. Choice/score heads:
    majority label, mean confidence, unstable when labels disagree.
    """
    valid = [d for d in draws if isinstance(d, dict) and d]
    merged: dict = {}
    head_ids: list = []
    for d in valid:
        for hid in d:
            if hid not in head_ids:
                head_ids.append(hid)
    for hid in head_ids:
        vals = [d[hid] for d in valid if isinstance(d.get(hid), dict) and not d[hid].get("_declined")]
        if not vals:
            merged[hid] = {"_declined": "no-answer", "confidence": 0.0}
            continue
        sample = dict(vals[0])
        nouls = [float(v["noul"]) for v in vals if isinstance(v.get("noul"), (int, float))]
        if nouls:
            mean = sum(nouls) / len(nouls)
            stdev = (sum((x - mean) ** 2 for x in nouls) / len(nouls)) ** 0.5 if len(nouls) > 1 else 0.0
            sample["noul"] = round(mean, 4)
            sample["mean"] = round(mean, 4)
            sample["stdev"] = round(stdev, 4)
            sample["unstable"] = len({x >= 0.5 for x in nouls}) > 1
            sample["draws"] = len(nouls)
        else:
            labels = [v.get("label") for v in vals if v.get("label") is not None]
            if labels:
                counts: dict = {}
                for lab in labels:
                    counts[lab] = counts.get(lab, 0) + 1
                sample["label"] = max(counts, key=lambda k: counts[k])
                confs = [float(v["confidence"]) for v in vals if isinstance(v.get("confidence"), (int, float))]
                if confs:
                    sample["confidence"] = round(sum(confs) / len(confs), 4)
                sample["stdev"] = 0.0 if len(counts) == 1 else 1.0
                sample["unstable"] = len(counts) > 1
                sample["draws"] = len(labels)
        merged[hid] = sample
    return merged


def _sys1_consistency(chain: list, task, state: str, cfg: dict, n: int = 3) -> tuple:
    """
    Plan-gate self-consistency: n draws per provider down the chain, first
    provider with any answers wins (same semantics as _sys1_classify_single).
    Returns (merged_answers, provider_used, total_latency_ms, draws_used,
    telemetry) — merged answers carry mean/stdev/unstable per head so the
    2026-10-02 jaggedness finding (3 runs, 3 different flag sets on identical
    input) becomes measurable per run instead of requiring three manual runs.
    """
    used_provider, total_ms, used_draws, used_telemetry = "", 0, 0, {}
    for pid in chain:
        mapped = _SYS1_PROVIDER_REMAP.get(pid, pid)
        draws: list = []
        ok = False
        for _ in range(max(1, n)):
            try:
                result = sys1.classify(pid, task, state, cfg=cfg, log=False)
            except Exception:
                break
            total_ms += result.latency_ms
            got = (result.answers or {}).get(mapped) or {}
            if got:
                ok = True
                used_telemetry = result.telemetry.get(mapped, {})
            draws.append(got)
        if ok:
            return _merge_draws(draws), mapped, float(total_ms), sum(1 for d in draws if d), used_telemetry
    return {}, chain[0] if chain else "", float(total_ms), 0, {}


def _diff_paths(diff: str) -> list:
    """Changed paths from diff --git headers (the b/ side)."""
    return [m.group(1) for m in re.finditer(r"^diff --git \S+ b/(.+)$", diff, re.M)]


_DOC_EXTS = (".md", ".rst", ".txt", ".adoc")


def _is_doc_path(path: str) -> bool:
    low = path.lower()
    return low.endswith(_DOC_EXTS) or low.startswith(("docs/", "documentation/"))


def _extract_labels(answers: dict[str, dict], *, prefer_key_substrings: tuple[str, ...] | None = None) -> list[str]:
    """
    Take sys1/dev-decisions answers {head_id: {label/score/noul}} and flatten
    to the set of unique string labels (skipping score/noul values unless they
    are strings). Used by cmd_pr_gate / cmd_triage_issues / cmd_changelog.
    """
    out: list[str] = []
    for head_id, entry in (answers or {}).items():
        if head_id.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        if prefer_key_substrings and not any(s.lower() in head_id.lower() for s in prefer_key_substrings):
            continue
        label = entry.get("label")
        if isinstance(label, list):
            out.extend(str(x) for x in label)
        elif label is not None:
            out.append(str(label))
    return sorted(set(out))


def _legacy_classify(provider: str, task: str, diff: str, cfg: dict) -> dict:
    """
    Inline provider path, preserved verbatim from pre-migration dev-decisions so
    the script still runs standalone when sys1 isn't installed.
    Returns the same shape as _sys1_classify.
    """
    from .config import DEFAULT_MAX_DIFF_CHARS

    heads = get_task_heads(task, provider.split("+")[0])
    results: dict[str, dict] = {}
    providers_used: list[str] = []
    provider_telemetry: dict[str, dict] = {}
    t0 = time.monotonic()

    if provider in ("decide", "both"):
        decide_key = _env_key("decide")
        if not decide_key:
            print("⚠ FASTINO_API_KEY not set — skipping Decide.", file=sys.stderr)
        else:
            try:
                messages = [
                    {"role": "system", "content": "You are a precise change classifier. Answer only with the requested labels."},
                    {"role": "user", "content": f"Review this git diff and classify it.\n\nDiff:\n{diff}"},
                ]
                pcfg = cfg["providers"]
                decide_telemetry: dict = {}
                resp = _call_openai_compatible(
                    pcfg["decide_api_url"],
                    decide_key,
                    pcfg["decide_model"],
                    messages,
                    schema={"classifications": heads},
                    timeout=pcfg["request_timeout_seconds"],
                    max_retries=pcfg["max_retries"],
                    backoff=pcfg["retry_backoff_seconds"],
                    telemetry=decide_telemetry,
                )
                parsed = _parse_decide_response(resp)
                results["decide"] = parsed
                providers_used.append("decide")
                provider_telemetry["decide"] = decide_telemetry
            except Exception as e:
                print(f"⚠ Decide call failed: {e}", file=sys.stderr)

    if provider in ("jev", "both"):
        jev_key = _env_key("jev")
        if not jev_key:
            print("⚠ TYPESAFE_API_KEY not set — skipping Jev.", file=sys.stderr)
        else:
            try:
                pcfg = cfg["providers"]
                questions = get_task_heads(task, "jev")
                payload_state = {"diff": diff[:DEFAULT_MAX_DIFF_CHARS], "task": task}
                payload_questions: dict[str, dict] = {}
                for q in questions:
                    payload_questions[q["id"]] = {
                        "type": q["type"],
                        "instructions": q["instructions"],
                        "criteria": q.get("criteria"),
                    }
                body = json.dumps({
                    "state": payload_state,
                    "questions": payload_questions,
                    "model": pcfg["jev_model"],
                })
                jev_telemetry: dict = {}
                raw = _call_jev_raw(body, jev_key, cfg, telemetry=jev_telemetry)
                parsed = _parse_jev_response(raw, [q["id"] for q in questions])
                results["jev"] = parsed
                providers_used.append("jev")
                provider_telemetry["jev"] = jev_telemetry
            except Exception as e:
                print(f"⚠ Jev call failed: {e}", file=sys.stderr)

    if provider in ("local", "both"):
        try:
            local_heads = get_task_heads(task, "local")
            local_telemetry: dict = {}
            parsed = _call_local_provider(diff, local_heads, cfg, telemetry=local_telemetry)
            results["local"] = parsed
            providers_used.append("local")
            provider_telemetry["local"] = local_telemetry
        except Exception as e:
            print(f"⚠ Local GLiNER call failed: {e}", file=sys.stderr)

    if provider == "modernbert":
        try:
            modernbert_heads = get_task_heads(task, "modernbert")
            modernbert_telemetry: dict = {}
            parsed = _call_modernbert_provider(diff, modernbert_heads, cfg, telemetry=modernbert_telemetry)
            results["modernbert_raw"] = parsed
            providers_used.append("modernbert_raw")
            provider_telemetry["modernbert_raw"] = modernbert_telemetry
        except Exception as e:
            print(f"⚠ ModernBERT eval failed: {e}", file=sys.stderr)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    escalated = False
    summary: list[str] = []
    for prov, heads_result in results.items():
        summary.append(f"\n── {prov} ──")
        for key, entry in heads_result.items():
            if key.startswith("_"):
                continue
            if not isinstance(entry, dict):
                summary.append(f"  {key}: (declined)")
                escalated = True
                continue
            label = entry.get("label") or entry.get("score") or entry.get("noul") or entry.get("_raw", "?")
            conf = entry.get("confidence")
            label_str = str(label)[:60]
            if conf is not None:
                summary.append(f"  {key}: {label_str} (conf {conf:.2f})")
            else:
                summary.append(f"  {key}: {label_str}")
            if conf is not None and conf < cfg["classify"]["confidence_floor"]:
                escalated = True
            if entry.get("label") is None and entry.get("score") is None and entry.get("noul") is None:
                escalated = True

    return {
        "results": results,
        "providers_used": providers_used,
        "telemetry": provider_telemetry,
        "escalated": escalated,
        "summary": summary,
        "latency_ms": elapsed_ms,
        "verdict": "escalate" if escalated else "pass",
    }



# ── provider clients ─────────────────────────────────────────────────────────

def _env_key(provider: str) -> str | None:
    if provider == "decide":
        return os.environ.get("FASTINO_API_KEY")
    if provider == "jev":
        return os.environ.get("TYPESAFE_API_KEY")
    return None


def _call_openai_compatible(
    api_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    schema: dict | None = None,
    timeout: int = 30,
    max_retries: int = 2,
    backoff: float = 5.0,
    telemetry: dict | None = None,
) -> dict:
    """
    Call an OpenAI-compatible chat-completions endpoint.
    Returns the parsed JSON response body, or raises on final failure.
    If `telemetry` dict is provided, populates it with call-level signals.
    """
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
    }
    if schema is not None:
        body["schema"] = schema

    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_err = ""
    t0 = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                api_url, data=data, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                http_status = resp.status
            parsed = json.loads(raw)
            # surface provider errors
            if "error" in parsed:
                msg = parsed["error"].get("message", str(parsed["error"]))
                if "warming" in msg.lower() and attempt < max_retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise RuntimeError(f"Provider error: {msg}")
            latency_ms = int((time.monotonic() - t0) * 1000)
            if telemetry is not None:
                telemetry["http_status"] = http_status
                telemetry["latency_ms"] = latency_ms
                telemetry["retries"] = attempt
                usage = parsed.get("usage") or {}
                telemetry["token_prompt"] = usage.get("prompt_tokens")
                telemetry["token_completion"] = usage.get("completion_tokens")
                telemetry["token_total"] = usage.get("total_tokens")
                # structured_output_success = we got a parseable body with choices
                telemetry["structured_ok"] = bool(parsed.get("choices"))
            return parsed
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {body_text}"
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
            else:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if telemetry is not None:
                    telemetry["http_status"] = e.code
                    telemetry["latency_ms"] = latency_ms
                    telemetry["retries"] = attempt
                    telemetry["structured_ok"] = False
                raise RuntimeError(last_err) from e
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
            else:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if telemetry is not None:
                    telemetry["latency_ms"] = latency_ms
                    telemetry["retries"] = attempt
                    telemetry["structured_ok"] = False
                raise RuntimeError(f"Request failed: {last_err}") from e

    latency_ms = int((time.monotonic() - t0) * 1000)
    if telemetry is not None:
        telemetry["latency_ms"] = latency_ms
        telemetry["retries"] = max_retries
        telemetry["structured_ok"] = False
    raise RuntimeError(last_err)


def _parse_decide_response(resp: dict) -> dict:
    """Extract {task: {label, confidence}} from Decide response."""
    content = resp["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # fallback: wrap raw content
        return {"_raw": content}


def _parse_jev_response(resp: dict, question_ids: list[str]) -> dict:
    """
    Parse Jev response. The OpenAI-compatible wrapper should return answers
    in one of these shapes:
      a) { "answers": { "<id>": { "type": ..., "choice"/"score"/"noul": ..., "confidence": ... } } }
      b) { "choices": [{"message": {"content": "<json>"}}] }  (JSON string in content)
    Returns {<id>: {label/score/noul, confidence}} keyed by question id.
    """
    # shape a: SDK-style
    if "answers" in resp:
        answers = resp["answers"]
        out: dict = {}
        for qid in question_ids:
            a = answers.get(qid, {})
            entry: dict = {"type": a.get("type", "unknown")}
            if a.get("type") == "choice":
                entry["label"] = a.get("choice")
                entry["confidence"] = a.get("confidence")
                entry["probabilities"] = a.get("probabilities")
            elif a.get("type") == "score":
                entry["score"] = a.get("score")
                entry["legend"] = a.get("legend")
                entry["confidence"] = a.get("confidence")
            elif a.get("type") == "noul":
                entry["noul"] = a.get("noul")
                entry["confidence"] = a.get("confidence")
            out[qid] = entry
        return out

    # shape b: chat-completions content
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "{}")
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "answers" in parsed:
            return _parse_jev_response(parsed, question_ids)
        return parsed
    except (json.JSONDecodeError, KeyError):
        return {"_raw": content}


def _build_local_heads() -> list[dict]:
    """Local GLiNER heads — same semantics as Decide for easy comparison."""
    return [
        {
            "task": "What type of change is this diff? Choose exactly one category.",
            "labels": _DIFF_TYPE_LABELS,
            "multi_label": False,
        },
        {
            "task": "What is the risk level of this change? low = safe, routine; medium = some behavior change; high = affects public APIs, data, auth, or invariants.",
            "labels": _RISK_LABELS,
            "multi_label": False,
        },
    ]


# ── task registry ──────────────────────────────────────────────────────────────
# Each task maps to provider-specific head definitions.
# Heads are built lazily so we don't pay for unused tasks.

def _build_commit_audit_heads() -> dict:
    """Does the commit message accurately describe the diff?"""
    return {
        "decide": [
            {"task": "How accurately does the commit message describe the diff? accurate = fully describes; partial = partially describes; misleading = contradicts or omits major changes.", "labels": ["accurate", "partial", "misleading"], "multi_label": False},
            {"task": "Does the commit message mention every major functional change in the diff?", "labels": ["yes", "no"], "multi_label": False},
        ],
        "jev": [
            {"id": "message_accuracy", "type": "choice", "instructions": "How accurately does the commit message describe the diff? accurate = fully describes; partial = partially describes; misleading = contradicts or omits major changes.", "criteria": {"accurate": "Fully describes the diff.", "partial": "Partially describes the diff.", "misleading": "Contradicts or omits major changes."}},
            {"id": "message_complete", "type": "noul", "instructions": "Does the commit message mention every major functional change in the diff?", "criteria": {"yes": "Yes, all major changes are mentioned.", "no": "No, major changes are missing."}},
        ],
        "local": [
            {"task": "How accurately does the commit message describe the diff? accurate = fully describes; partial = partially describes; misleading = contradicts or omits major changes.", "labels": ["accurate", "partial", "misleading"], "multi_label": False},
        ],
    }


def _build_deps_risk_heads() -> dict:
    """Classify dependency update bump level and risk."""
    return {
        "decide": [
            {"task": "What is the semver bump level of this dependency update? patch = bug fix; minor = new feature, backward-compatible; major = breaking change.", "labels": ["patch", "minor", "major"], "multi_label": False},
            {"task": "What is the risk level? low = patch or minor in low-traffic path; medium = minor in core path or major in low-traffic; high = major in core path or affects auth/data.", "labels": _RISK_LABELS, "multi_label": False},
            {"task": "Does this dependency update introduce a breaking change?", "labels": _BREAKING_LABELS, "multi_label": False},
        ],
        "jev": [
            {"id": "bump_level", "type": "choice", "instructions": "What is the semver bump level? patch = bug fix; minor = new feature, backward-compatible; major = breaking change.", "criteria": {"patch": "Patch-level bug fix.", "minor": "Minor version, new feature, backward-compatible.", "major": "Major version, breaking change."}},
            {"id": "risk_tier", "type": "choice", "instructions": "What is the risk level? low = patch or minor in low-traffic path; medium = minor in core path or major in low-traffic; high = major in core path or affects auth/data.", "criteria": {"low": "Low risk.", "medium": "Medium risk.", "high": "High risk."}},
            {"id": "breaking_change", "type": "noul", "instructions": "Does this dependency update introduce a breaking change?", "criteria": {"yes": "Yes, breaking change.", "no": "No breaking change."}},
        ],
        "local": [
            {"task": "What is the semver bump level of this dependency update? patch = bug fix; minor = new feature, backward-compatible; major = breaking change.", "labels": ["patch", "minor", "major"], "multi_label": False},
            {"task": "What is the risk level? low = patch or minor in low-traffic path; medium = minor in core path or major in low-traffic; high = major in core path or affects auth/data.", "labels": _RISK_LABELS, "multi_label": False},
        ],
    }


def _build_docs_drift_heads() -> dict:
    """Detect docs drift: docs changes without corresponding code changes."""
    return {
        "decide": [
            {"task": "Does this diff include code changes (not just docs)?", "labels": ["code-only", "docs-only", "mixed"], "multi_label": False},
            {"task": "Is there documentation drift — code changed without corresponding doc updates, or docs updated without code changes?", "labels": ["no-drift", "docs-need-update", "code-needs-docs"], "multi_label": False},
        ],
        "jev": [
            {"id": "change_mix", "type": "choice", "instructions": "Does this diff include code changes (not just docs)?", "criteria": {"code-only": "Only code changes.", "docs-only": "Only documentation changes.", "mixed": "Both code and docs changes."}},
            {"id": "docs_drift", "type": "noul", "instructions": "Is there documentation drift — code changed without corresponding doc updates, or docs updated without code changes?", "criteria": {"yes": "Yes, docs drift detected.", "no": "No docs drift."}},
        ],
        "local": [
            {"task": "Does this diff include code changes (not just docs)?", "labels": ["code-only", "docs-only", "mixed"], "multi_label": False},
            {"task": "Is there documentation drift?", "labels": ["no-drift", "drift"], "multi_label": False},
        ],
    }


def _build_api_drift_heads() -> dict:
    """Detect API contract drift / breaking changes."""
    return {
        "decide": [
            {"task": "Does this diff modify any public API surface (routes, schemas, exported functions, types, interfaces, RPC methods)?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "Does this diff introduce a breaking change to the public API?", "labels": _BREAKING_LABELS, "multi_label": False},
            {"task": "If there is a breaking change, how severe is it? high = requires immediate migration; medium = requires migration but has deprecation path; low = backward-compatible.", "labels": ["high", "medium", "low", "none"], "multi_label": False},
        ],
        "jev": [
            {"id": "public_api_modified", "type": "noul", "instructions": "Does this diff modify any public API surface (routes, schemas, exported functions, types, interfaces, RPC methods)?", "criteria": {"yes": "Yes, public API modified.", "no": "No public API modified."}},
            {"id": "breaking_change", "type": "noul", "instructions": "Does this diff introduce a breaking change to the public API?", "criteria": {"yes": "Yes, breaking change.", "no": "No breaking change."}},
            {"id": "severity", "type": "choice", "instructions": "If there is a breaking change, how severe is it?", "criteria": {"high": "High — requires immediate migration.", "medium": "Medium — requires migration but has deprecation path.", "low": "Low — backward-compatible.", "none": "None."}},
        ],
        "local": [
            {"task": "Does this diff modify any public API surface (routes, schemas, exported functions, types, interfaces, RPC methods)?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "Does this diff introduce a breaking change?", "labels": _BREAKING_LABELS, "multi_label": False},
        ],
    }


def _build_pr_gate_heads() -> dict:
    """PR quality gate: change type, risk, suggested labels."""
    _PR_LABELS = ["bug", "feature", "refactor", "docs", "chore", "test", "ci"]
    return {
        "decide": [
            {"task": "What type of change is this PR? Choose the best fit.", "labels": _DIFF_TYPE_LABELS, "multi_label": False},
            {"task": "What is the risk level? low = safe; medium = behavior change; high = public API, data, auth, or invariant change.", "labels": _RISK_LABELS, "multi_label": False},
            {"task": "Which labels should be applied? Pick all that apply.", "labels": _PR_LABELS, "multi_label": True},
        ],
        "jev": [
            {"id": "diff_type", "type": "choice", "instructions": "What type of change is this PR?", "criteria": {l: f"A {l} change." for l in _DIFF_TYPE_LABELS}},
            {"id": "risk_tier", "type": "choice", "instructions": "What is the risk level?", "criteria": {l: f"Risk level {l}." for l in _RISK_LABELS}},
            {"id": "suggested_labels", "type": "choice", "instructions": "Which labels should be applied?", "criteria": {l: f"Apply {l} label." for l in _PR_LABELS}},
        ],
        "local": [
            {"task": "What type of change is this PR?", "labels": _DIFF_TYPE_LABELS, "multi_label": False},
            {"task": "What is the risk level?", "labels": _RISK_LABELS, "multi_label": False},
            {"task": "Which labels should be applied?", "labels": _PR_LABELS, "multi_label": True},
        ],
    }


def _build_issue_triage_heads() -> dict:
    """Issue triage: kind and priority."""
    _ISSUE_KINDS = ["bug", "feature", "docs", "question", "chore"]
    _ISSUE_PRIORITIES = ["low", "medium", "high", "critical"]
    return {
        "decide": [
            {"task": "What kind of issue is this?", "labels": _ISSUE_KINDS, "multi_label": False},
            {"task": "What priority should this issue have?", "labels": _ISSUE_PRIORITIES, "multi_label": False},
        ],
        "jev": [
            {"id": "kind", "type": "choice", "instructions": "What kind of issue is this?", "criteria": {k: f"This is a {k}." for k in _ISSUE_KINDS}},
            {"id": "priority", "type": "choice", "instructions": "What priority should this issue have?", "criteria": {p: f"Priority is {p}." for p in _ISSUE_PRIORITIES}},
        ],
        "local": [
            {"task": "What kind of issue is this?", "labels": _ISSUE_KINDS, "multi_label": False},
            {"task": "What priority should this issue have?", "labels": _ISSUE_PRIORITIES, "multi_label": False},
        ],
    }


def _build_safety_heads() -> dict:
    """Destructive-command safety check."""
    return {
        "decide": [
            {"task": "Does this command perform a destructive operation (force push, rm -rf, DROP/TRUNCATE, destructive migration against production, --no-verify bypass)?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "If destructive, is it reversible?", "labels": ["yes", "no"], "multi_label": False},
        ],
        "jev": [
            {"id": "destructive", "type": "noul", "instructions": "Does this command perform a destructive operation (force push, rm -rf, DROP/TRUNCATE, destructive migration against production, --no-verify bypass)?", "criteria": {"yes": "Yes, this is destructive.", "no": "Not destructive."}},
            {"id": "reversible", "type": "noul", "instructions": "If destructive, is it reversible?", "criteria": {"yes": "Yes, reversible.", "no": "No, irreversible."}},
        ],
        "local": [
            {"task": "Does this command perform a destructive operation?", "labels": ["yes", "no"], "multi_label": False},
            {"task": "If destructive, is it reversible?", "labels": ["yes", "no"], "multi_label": False},
        ],
    }


# task name → builder function
_TASK_BUILDERS: dict[str, Callable[[], dict]] = {
    "change": lambda: {
        "decide": _build_decide_heads(),
        "jev": _build_jev_heads(),
        "local": _build_local_heads(),
        "modernbert": _build_local_heads(),  # eval: same label set, different model
    },
    "commit_audit": lambda: {
        **_build_commit_audit_heads(),
        "modernbert": _build_commit_audit_heads().get("local", []),
    },
    "deps_risk": lambda: {
        **_build_deps_risk_heads(),
        "modernbert": _build_deps_risk_heads().get("local", []),
    },
    "docs_drift": lambda: {
        **_build_docs_drift_heads(),
        "modernbert": _build_docs_drift_heads().get("local", []),
    },
    "api_drift": lambda: {
        **_build_api_drift_heads(),
        "modernbert": _build_api_drift_heads().get("local", []),
    },
    "pr_gate": lambda: {
        **_build_pr_gate_heads(),
        "modernbert": _build_pr_gate_heads().get("local", []),
    },
    "issue_triage": lambda: {
        **_build_issue_triage_heads(),
        "modernbert": _build_issue_triage_heads().get("local", []),
    },
    "safety": _build_safety_heads,  # no modernbert: regex-driven, no training data yet
}


def get_task_heads(task: str, provider: str) -> list[dict]:
    """Return provider-specific heads for a named task."""
    task_def = _TASK_BUILDERS.get(task, _TASK_BUILDERS["change"])()
    return task_def.get(provider, task_def.get("decide", []))


def detect_task_from_diff(diff: str, paths: list | None = None) -> str:
    paths = paths if paths is not None else _diff_paths(diff)
    diff_lower = diff.lower()
    dep_patterns = ["package.json", "package-lock.json", "yarn.lock", "requirements.txt",
                    "pyproject.toml", "Pipfile", "poetry.lock", "pom.xml", "build.gradle",
                    "Cargo.toml", "go.mod", "composer.json", "Gemfile"]
    # dep manifests are matched on CHANGED PATHS (a README mentioning
    # package.json used to route here via body text)
    if any(any(d in pp.lower() for d in dep_patterns) for pp in paths):
        return "deps_risk"
    # docs-only = every changed path is a doc file (was: any ".md" anywhere in
    # the diff body, which code diffs mentioning a markdown path also hit)
    if paths and all(_is_doc_path(pp) for pp in paths):
        return "docs_drift"
    return "change"

def _call_jev_raw(body: str, api_key: str | None, cfg: dict, telemetry: dict | None = None) -> dict:
    """
    Call Jev /v1/systemone and return the parsed JSON response dict.
    Retries on transient errors. Raises RuntimeError on failure.
    If `telemetry` dict is provided, populates it with HTTP-level signals.
    """
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY not set")

    url = cfg["providers"]["jev_api_url"]
    timeout = cfg["providers"]["request_timeout_seconds"]
    max_retries = cfg["providers"]["max_retries"]
    backoff = cfg["providers"]["retry_backoff_seconds"]

    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body.encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                http_status = resp.status
            latency_ms = int((time.monotonic() - t0) * 1000)
            if telemetry is not None:
                telemetry["http_status"] = http_status
                telemetry["latency_ms"] = latency_ms
                telemetry["retries"] = attempt
            try:
                return json.loads(raw_bytes.decode())
            except json.JSONDecodeError:
                if telemetry is not None:
                    telemetry["structured_ok"] = False
                raise RuntimeError(f"Jev returned non-JSON: {raw_bytes[:200]}")
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode()[:300]}"
            if telemetry is not None:
                telemetry["http_status"] = e.code
                telemetry["latency_ms"] = int((time.monotonic() - t0) * 1000)
                telemetry["retries"] = attempt
                telemetry["structured_ok"] = False
            if e.code >= 500 and attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError(f"Jev HTTP error: {last_err}")
        except Exception as e:
            last_err = str(e)
            if telemetry is not None:
                telemetry["latency_ms"] = int((time.monotonic() - t0) * 1000)
                telemetry["retries"] = attempt
                telemetry["structured_ok"] = False
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError(f"Jev call failed: {last_err}")
    if telemetry is not None:
        telemetry["latency_ms"] = int((time.monotonic() - t0) * 1000)
        telemetry["retries"] = max_retries
        telemetry["structured_ok"] = False
    raise RuntimeError(f"Jev exhausted retries: {last_err}")


def _call_modernbert_provider(text: str, heads: list[dict], cfg: dict, telemetry: dict | None = None) -> dict:
    """
    Experimental ModernBERT eval provider (no fine-tuning).

    Uses ModernBERT-base as a sentence encoder: encodes the input text once,
    then scores each candidate label by cosine similarity against the label's
    standalone embedding. Returns {task_text: {label, confidence, all_scores}}
    or raises. all_scores is kept for calibration dashboards.

    Results are logged with provider tag 'modernbert_raw' so they can be
    filtered from production metrics and used to understand what fine-tuning
    would need to improve.
    """
    import math

    venv_python = Path(cfg["providers"].get("local_venv", "/private/tmp/gliner-decide")) / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"Local venv not found at {venv_python}")

    model_name = cfg["providers"].get("modernbert_model", "answerdotai/ModernBERT-base")
    max_chars = cfg["scan"]["max_diff_chars"]
    text_truncated = text[:max_chars]

    # Build inline script that runs inside the venv
    inline = f'''
import json, sys, math
import numpy as np
from transformers import AutoTokenizer, AutoModel

model_name = {model_name!r}
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModel.from_pretrained(model_name)
model.eval()

def to_python(val):
    """Convert numpy types to native Python for JSON serialization."""
    if hasattr(val, "item"):
        return val.item()
    return val

def cosine(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

def encode(text):
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=8192, padding=False)
    with torch.no_grad():
        outputs = model(**inputs)
    # [CLS] token embedding (index 0)
    cls = outputs.last_hidden_state[0, 0, :].numpy()
    # Normalize
    norm = np.linalg.norm(cls)
    if norm > 0:
        cls = cls / norm
    return cls

try:
    text_emb = encode(sys.argv[1])
    heads = json.loads(sys.argv[2])
    results = {{}}
    for tid, task in heads.items():
        labels = task.get("labels", [])
        if not labels:
            results[tid] = {{"label": None, "confidence": 0.0}}
            continue
        # Score each label by similarity to its standalone embedding
        scores = []
        for label in labels:
            label_emb = encode(label)
            scores.append(cosine(text_emb, label_emb))
        best_idx = max(range(len(scores)), key=lambda i: scores[i])
        best_label = labels[best_idx]
        best_score = scores[best_idx]
        # Compute top2 gap for calibration
        sorted_scores = sorted(scores, reverse=True)
        top2_gap = float(sorted_scores[0] - sorted_scores[1]) if len(sorted_scores) > 1 else 1.0
        results[tid] = {{
            "label": to_python(best_label),
            "confidence": to_python(round(best_score, 4)),
            "all_scores": {{to_python(l): to_python(round(s, 4)) for l, s in zip(labels, scores)}},
            "top2_gap": to_python(round(top2_gap, 4)),
            "embedding_norm": to_python(round(float(np.linalg.norm(text_emb)), 4)),
        }}
    print(json.dumps(results))
except Exception as e:
    print(json.dumps({{"_error": str(e)}}))
'''

    # Need torch in the inline script too
    inline = "import torch\n" + inline

    tasks_json = json.dumps({h["task"][:40]: {"labels": h.get("labels", [])} for h in heads})

    t0 = time.monotonic()
    proc = subprocess.run(
        [str(venv_python), "-c", inline, text_truncated, tasks_json],
        capture_output=True,
        text=True,
        timeout=cfg["providers"]["request_timeout_seconds"] * 2,  # ModernBERT is slower on CPU
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    if telemetry is not None:
        telemetry["latency_ms"] = latency_ms
        if proc.returncode != 0:
            stderr = (proc.stderr or "")[:300]
            telemetry["error_kind"] = "timeout" if "timeout" in stderr.lower() else "model_load" if "model" in stderr.lower() else "other"
            telemetry["stderr"] = stderr

    if proc.returncode != 0:
        raise RuntimeError(f"ModernBERT eval failed: {proc.stderr[:300]}")

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        parsed = {"_raw": proc.stdout[:200]}
        if telemetry is not None:
            telemetry["error_kind"] = "json"

    # Normalize output shape to match other providers
    out: dict = {}
    for tid, entry in parsed.items():
        if isinstance(entry, dict) and "_error" not in entry:
            # Cast numpy types to native Python for JSON safety
            label = entry.get("label")
            conf = entry.get("confidence")
            if hasattr(conf, "item"):  # numpy scalar
                conf = conf.item()
            if hasattr(label, "item"):
                label = label.item()
            out[tid] = {
                "label": label,
                "confidence": conf,
                # keep all_scores, top2_gap, embedding_norm for dashboards
                **{k: v for k, v in entry.items() if k in ("all_scores", "top2_gap", "embedding_norm")},
            }
        else:
            out[tid] = {"_raw": str(entry)}
    return out


def _call_local_provider(diff: str, heads: list[dict], cfg: dict, telemetry: dict | None = None) -> dict:
    """
    Call local GLiNER2 via the existing /private/tmp/gliner-decide venv.
    Returns {task_text: {label, confidence}} or raises on failure.
    If `telemetry` dict is provided, populates it with subprocess signals.
    """
    venv_python = Path(cfg["providers"].get("local_venv", "/private/tmp/gliner-decide")) / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"Local GLiNER venv not found at {venv_python}")

    # Build the inline script that runs inside the venv
    tasks_json = json.dumps({h["task"][:40]: {"labels": h.get("labels", []), "instruction": h["task"]} for h in heads})
    inline = f'''
import json, sys, io

# Suppress model init banner
old_stdout = sys.stdout
sys.stdout = io.StringIO()

from gliner2.classification import ClassificationConfig, ClassificationSchema, Classifier

model_name = {cfg["providers"]["local_model"]!r}
text = sys.argv[1]
tasks = json.loads(sys.argv[2])

# Fastino-prescribed usage (gliner25-decide-playground/model.py): one
# ClassificationSchema holding every task, one decode, independent decoder —
# the same surface sys1's local wire uses, so the two agree. instruction
# carries the head's question text; without it the model only sees labels.
schema = ClassificationSchema()
for name, spec in tasks.items():
    schema.single(name, spec["labels"], instruction=spec.get("instruction"))
clf = Classifier.from_pretrained(model_name, map_location="cpu")
result = clf.classify(text, schema, config=ClassificationConfig(decoder="independent", on_infeasible="relax"))

out = {{}}
for name in tasks:
    try:
        value = result.value(name)
        probs = result.probabilities(name)
    except Exception:
        continue  # task absent from the result -> missing below
    labels = list(value) if isinstance(value, (list, tuple)) else [value]
    out[name] = {{
        "label": str(labels[0]) if labels else None,
        "confidence": max(probs.values()) if probs else None,
    }}

# Restore stdout and print only the JSON result
sys.stdout = old_stdout
print(json.dumps(out))
'''

    t0 = time.monotonic()
    proc = subprocess.run(
        [str(venv_python), "-c", inline, diff[: cfg["scan"]["max_diff_chars"]], tasks_json],
        capture_output=True, text=True, timeout=cfg["providers"]["request_timeout_seconds"],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    if telemetry is not None:
        telemetry["latency_ms"] = latency_ms
        if proc.returncode != 0:
            telemetry["error_kind"] = "other"
            telemetry["stderr"] = (proc.stderr or "")[:300]

    if proc.returncode != 0:
        raise RuntimeError(f"Local GLiNER failed: {proc.stderr[:300]}")

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        parsed = {"_raw": proc.stdout[:200]}
        if telemetry is not None:
            telemetry["error_kind"] = "json"

    # Normalize: keys are task[:40] from caller
    out: dict = {}
    null_count = 0
    for tid, entry in parsed.items():
        if isinstance(entry, dict):
            label = entry.get("label")
            if label is None:
                null_count += 1
            out[tid] = {
                "label": label,
                "confidence": entry.get("confidence"),
            }
        else:
            out[tid] = {"_raw": str(entry)}
    if telemetry is not None:
        telemetry["null_label_count"] = null_count
    return out


def _call_local_pii_scan(diff: str, cfg: dict) -> dict:
    """
    Run local GLiNER2 PII span extraction via the existing venv.
    Takes a raw git diff, strips git metadata, and scans content text.
    Returns {entity_type: [text, ...]} or raises on failure.
    """
    venv_python = Path(cfg["providers"].get("local_venv", "/private/tmp/gliner-decide")) / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"Local GLiNER venv not found at {venv_python}")

    pii_model = cfg["providers"].get("pii_model", "fastino/gliner2-privacy-filter-PII-multi")
    pii_labels = cfg.get("scan", {}).get("pii_labels", ["person", "email", "phone", "ssn", "address"])

    # Strip git diff metadata — only scan actual content
    content = diff_content_text(diff)
    if not content.strip():
        return {"entities": {}}

    inline = f'''
import json, sys, io, contextlib
from gliner2 import GLiNER2

# Suppress model init banner
old_stdout = sys.stdout
sys.stdout = io.StringIO()

model = GLiNER2.from_pretrained({pii_model!r})
text = sys.argv[1]
labels = json.loads(sys.argv[2])
schema = model.create_schema().entities(labels).build()
result = model.extract(text, schema)

# Restore stdout and print only the JSON result
sys.stdout = old_stdout
print(json.dumps(result))
'''

    proc = subprocess.run(
        [str(venv_python), "-c", inline, content[: cfg["scan"]["max_diff_chars"]], json.dumps(pii_labels)],
        capture_output=True, text=True, timeout=cfg["providers"]["request_timeout_seconds"],
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Local PII scan failed: {proc.stderr[:300]}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_raw": proc.stdout[:200]}


# ── classify-diff heads ──────────────────────────────────────────────────────

_DIFF_TYPE_LABELS = ["feat", "fix", "refactor", "docs", "test", "chore", "deps", "style"]
_RISK_LABELS = ["low", "medium", "high"]
_BREAKING_LABELS = ["yes", "no"]
_PII_IN_DIFF_LABELS = ["yes", "no"]


def _build_decide_heads() -> list[dict]:
    return [
        {
            "task": "What type of change is this diff? Choose exactly one category.",
            "labels": _DIFF_TYPE_LABELS,
            "multi_label": False,
        },
        {
            "task": "What is the risk level of this change? low = safe, routine; medium = some behavior change; high = affects public APIs, data, auth, or invariants.",
            "labels": _RISK_LABELS,
            "multi_label": False,
        },
    ]


def _build_jev_heads() -> list[dict]:
    # Jev-style Choice questions with criteria descriptions (first-class).
    return [
        {
            "id": "diff_type",
            "type": "choice",
            "instructions": "What type of change is this diff? Choose exactly one category.",
            "criteria": {l: f"A {l} change." for l in _DIFF_TYPE_LABELS},
        },
        {
            "id": "risk_tier",
            "type": "choice",
            "instructions": "What is the risk level? low = safe routine; medium = some behavior change; high = public API, data, auth, or invariant change.",
            "criteria": {l: f"Risk level {l}." for l in _RISK_LABELS},
        },
        {
            "id": "breaking_change",
            "type": "noul",
            "instructions": "Does this diff introduce a breaking change — removes a public API, changes a response schema, or requires migration?",
            "criteria": {
                "yes": "Yes, this is a breaking change.",
                "no": "No breaking change.",
            },
        },
        {
            "id": "personal_data",
            "type": "noul",
            "instructions": "Does the diff add or modify code that handles personal data (PII, student records, health, financial)?",
            "criteria": {
                "yes": "Yes, personal data is involved.",
                "no": "No personal data.",
            },
        },
    ]


