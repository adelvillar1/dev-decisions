"""local dashboard server."""

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

from .config import EXIT_ERROR, EXIT_OK, LOG_DIR

# section: dashboard (moved verbatim from dev_decisions.py)

# ── dashboard ─────────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dev-decisions dashboard</title>
<style>
  :root {
    --bg: #0b0f19;
    --panel: #111827;
    --border: #1f2937;
    --text: #e5e7eb;
    --muted: #9ca3af;
    --accent: #60a5fa;
    --danger: #f87171;
    --warn: #fbbf24;
    --success: #34d399;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    padding: 18px 24px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, rgba(17,24,39,.9), rgba(17,24,39,.6));
  }
  header h1 {
    margin: 0 0 6px 0;
    font-size: 18px;
    letter-spacing: .2px;
  }
  header p { margin: 0; color: var(--muted); font-size: 12px; }
  main {
    padding: 18px;
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 16px;
  }
  .panel {
    grid-column: span 12;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
  }
  .panel h2 {
    margin: 0 0 10px 0;
    font-size: 13px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .6px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    font-family: var(--mono);
  }
  th, td { padding: 7px 9px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  tr:last-child td { border-bottom: none; }
  .badge {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 999px;
    font-size: 11px;
    background: #1f2937;
    border: 1px solid #374151;
  }
  .ok { color: var(--success); }
  .warn { color: var(--warn); }
  .err { color: var(--danger); }
  .muted { color: var(--muted); }
  .bar { height: 8px; border-radius: 4px; background: #1f2937; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: var(--accent); }
  code { font-family: var(--mono); font-size: 11px; color: #c7d2fe; background: #0b1220; padding: 2px 6px; border-radius: 4px; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .col { flex: 1 1 280px; min-width: 260px; }
  @media (max-width: 980px) {
    main { grid-template-columns: 1fr; }
    .panel { grid-column: span 1; }
  }
</style>
</head>
<body>
<header>
  <h1>dev-decisions</h1>
  <p>provider telemetry • calibration • modernbert eval</p>
</header>
<main>
  <section class="panel">
    <h2>provider health</h2>
    <div id="summary">loading…</div>
  </section>

  <section class="panel">
    <h2>confidence histograms</h2>
    <div id="confidence">loading…</div>
  </section>

  <section class="panel">
    <h2>modernbert signals</h2>
    <div id="modernbert">loading…</div>
  </section>

  <section class="panel">
    <h2>cross-provider agreement</h2>
    <div id="agreement">loading…</div>
  </section>

  <section class="panel">
    <h2>calibration (human feedback)</h2>
    <div id="calibration">loading…</div>
  </section>
</main>

<script>
function el(tag, cls, text){ const e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function num(v){ const n=Number(v); return Number.isFinite(n)?n:null; }
function fmt(n){ if(n===null) return '—'; if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1e3) return (n/1e3).toFixed(1)+'k'; return String(n); }
function pct(n){ if(n===null) return '—'; return (n*100).toFixed(1)+'%'; }
function bar(pct){ const d=document.createElement('div'); d.className='bar'; const i=document.createElement('i'); i.style.width=pct; d.appendChild(i); return d; }

async function api(path){
  const r=await fetch(path); if(!r.ok) throw new Error(r.status+' '+r.statusText); return r.json();
}

function renderSummary(data){
  const wrap=document.getElementById('summary');
  wrap.innerHTML='';
  if(!data.providers||!data.providers.length){ wrap.textContent='no data'; return; }
  const row=document.createElement('div'); row.className='row';
  for(const p of data.providers){
    const col=document.createElement('div'); col.className='col panel';
    col.style.background='#0b1220';
    col.style.border='1px solid #1f2937';
    col.innerHTML=`<h2 style="margin-top:0"><code>${p.name}</code> <span class="badge">${fmt(p.calls)} calls</span></h2>`;
    const stats = document.createElement('div');
    stats.style.cssText = 'font-size:12px;margin-bottom:10px;';
    stats.innerHTML = `err <code>${pct(p.error_rate)}</code> · p50 <code>${p.latency_p50?p.latency_p50.toFixed(0)+'ms':'—'}</code> · p95 <code>${p.latency_p95?p.latency_p95.toFixed(0)+'ms':'—'}</code> · null <code>${pct(p.null_rate)}</code>`;
    col.appendChild(stats);
    if(p.latency_bins && p.latency_bins.length){
      const h = document.createElement('div');
      h.innerHTML='<div style="font-size:11px;color:var(--muted);margin-bottom:6px">latency distribution</div>';
      const max = Math.max(...p.latency_bins.map(b=>b.count), 1);
      for(const b of p.latency_bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/max)*100));
        h.appendChild(line);
      }
      col.appendChild(h);
    }
    row.appendChild(col);
  }
  wrap.appendChild(row);
}

function renderConfidence(data){
  const wrap=document.getElementById('confidence');
  wrap.innerHTML='';
  if(!data.providers||!data.providers.length){ wrap.textContent='no data'; return; }
  const row=document.createElement('div'); row.className='row';
  for(const p of data.providers){
    const col=document.createElement('div'); col.className='col panel';
    col.style.background='#0b1220';
    col.style.border='1px solid #1f2937';
    col.innerHTML=`<h2 style="margin-top:0"><code>${p.name}</code> <span class="badge">${p.calls} calls</span></h2>`;
    const bins = (p.fine_bins && p.fine_bins.length) ? p.fine_bins : p.bins;
    const label = (p.fine_bins && p.fine_bins.length) ? 'confidence (fine)' : 'confidence';
    const title = document.createElement('div');
    title.style.cssText = 'font-size:11px;color:var(--muted);margin-bottom:8px;';
    title.textContent = label;
    col.appendChild(title);
    if(!bins||!bins.length){ col.innerHTML+='<div class="muted">no confidence data</div>'; }
    else {
      const max=Math.max(...bins.map(b=>b.count));
      for(const b of bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/Math.max(max,1))*100));
        col.appendChild(line);
      }
    }
    row.appendChild(col);
  }
  wrap.appendChild(row);
}

function renderModernbert(data){
  const wrap=document.getElementById('modernbert');
  wrap.innerHTML='';
  if(!data.tasks||!data.tasks.length){ wrap.innerHTML='<div class="muted">no modernbert data yet</div>'; return; }
  for(const t of data.tasks){
    const section = document.createElement('div');
    section.className='panel';
    section.style.marginBottom='12px';
    section.innerHTML=`<h2 style="margin-top:0"><code>${t.name}</code> <span class="badge">${fmt(t.calls)} calls</span></h2>`;
    const meta = document.createElement('div');
    meta.style.cssText = 'font-size:12px;margin-bottom:10px;';
    meta.innerHTML = `avg top2-gap <code>${t.avg_top2===null?'—':t.avg_top2.toFixed(3)}</code> · embedding norm <code>${t.avg_norm===null?'—':t.avg_norm.toFixed(2)}</code>`;
    section.appendChild(meta);
    if(t.top2_bins && t.top2_bins.length){
      const h = document.createElement('div');
      h.innerHTML='<div style="font-size:11px;color:var(--muted);margin-bottom:6px">top2-gap spread</div>';
      const max = Math.max(...t.top2_bins.map(b=>b.count), 1);
      for(const b of t.top2_bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/max)*100));
        h.appendChild(line);
      }
      section.appendChild(h);
    }
    if(t.norm_bins && t.norm_bins.length){
      const h = document.createElement('div');
      h.innerHTML='<div style="font-size:11px;color:var(--muted);margin:10px 0 6px">embedding-norm spread</div>';
      const max = Math.max(...t.norm_bins.map(b=>b.count), 1);
      for(const b of t.norm_bins){
        const line=document.createElement('div');
        line.style.marginBottom='6px';
        line.innerHTML=`<div style="display:flex;justify-content:space-between;font-size:11px"><span>${b.bin}</span><span>${b.count}</span></div>`;
        line.appendChild(bar((b.count/max)*100));
        h.appendChild(line);
      }
      section.appendChild(h);
    }
    const top=(t.top_labels||[]).map(x=>`${x.label}(${x.count})`).join(', ') || '—';
    const labels = document.createElement('div');
    labels.style.cssText = 'font-size:12px;margin-top:8px;';
    labels.innerHTML = `<span class="muted">top labels:</span> ${top}`;
    section.appendChild(labels);
    wrap.appendChild(section);
  }
}

function renderAgreement(data){
  const wrap=document.getElementById('agreement');
  wrap.innerHTML='';
  if(!data.pairs||!data.pairs.length){ wrap.innerHTML='<div class="muted">no cross-provider data yet</div>'; return; }
  const tbl=document.createElement('table');
  tbl.innerHTML=`<thead><tr><th>pair</th><th>compared</th><th>agree</th><th>rate</th></tr></thead>`;
  const body=document.createElement('tbody');
  for(const p of data.pairs){
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td><code>${p.pair}</code></td>
      <td>${fmt(p.compared)}</td>
      <td>${fmt(p.agree)}</td>
      <td>${pct(p.rate)}</td>
    `;
    body.appendChild(tr);
  }
  tbl.appendChild(body);
  wrap.appendChild(tbl);
}

function renderCalibration(data){
  const wrap=document.getElementById('calibration');
  wrap.innerHTML='';
  if(!data.bins||!data.bins.length){ wrap.innerHTML='<div class="muted">no feedback yet. use <code>dev-decisions feedback</code></div>'; return; }
  const tbl=document.createElement('table');
  tbl.innerHTML=`<thead><tr><th>provider</th><th>conf bin</th><th>samples</th><th>correct</th><th>accuracy</th></tr></thead>`;
  const body=document.createElement('tbody');
  for(const b of data.bins){
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td><code>${b.provider}</code></td>
      <td>${b.bin}</td>
      <td>${fmt(b.samples)}</td>
      <td>${fmt(b.correct)}</td>
      <td class="${(b.accuracy||0)>=0.8?'ok':(b.accuracy||0)>=0.6?'warn':'err'}">${pct(b.accuracy)}</td>
    `;
    body.appendChild(tr);
  }
  tbl.appendChild(body);
  wrap.appendChild(tbl);
}

async function init(){
  try {
    const [summary, confidence, modernbert, agreement, calibration] = await Promise.all([
      api('/api/summary?days=7'),
      api('/api/confidence?days=7'),
      api('/api/modernbert?days=7'),
      api('/api/agreement?days=7'),
      api('/api/calibration?days=7'),
    ]);
    renderSummary(summary);
    renderConfidence(confidence);
    renderModernbert(modernbert);
    renderAgreement(agreement);
    renderCalibration(calibration);
  } catch (e) {
    document.body.innerHTML='<main class="panel"><h2>dashboard error</h2><pre>'+e+'</pre></main>';
  }
}
init();
</script>
</body>
</html>
"""


class _DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, days: int = 7, base: Path = LOG_DIR, **kwargs):
        self._days = days
        self._base = base
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass

    def _read_jsonl(self, relpath: str):
        path = self._base / relpath
        if not path.exists():
            return []
        records = []
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _filter_days(self, records):
        cutoff = datetime.now(timezone.utc).timestamp() - (self._days * 86400)
        out = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.get("ts", "")).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                out.append(r)
        return out

    def _json(self, payload, code=200):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _today_events_path(self) -> Path:
        """Return the expected events.jsonl path for today (UTC)."""
        return self._base / datetime.now(timezone.utc).strftime("%Y/%m/%d") / "events.jsonl"

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_DASHBOARD_HTML.encode())))
            self.end_headers()
            self.wfile.write(_DASHBOARD_HTML.encode())
            return
        if self.path.startswith("/api/summary"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            # fallback: read all dated dirs
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            providers = {}
            for r in records:
                for prov, tel in (r.get("telemetry") or {}).items():
                    bucket = providers.setdefault(prov, {"calls": 0, "errors": 0, "latencies": [], "nulls": 0, "nouls": 0, "structured": 0})
                    bucket["calls"] += 1
                    if tel.get("error_kind") or tel.get("http_status", 200) >= 400:
                        bucket["errors"] += 1
                    if tel.get("latency_ms") is not None:
                        bucket["latencies"].append(tel["latency_ms"])
                    if tel.get("null_label_count"):
                        bucket["nulls"] += tel["null_label_count"]
                    if tel.get("noul_count"):
                        bucket["nouls"] += tel["noul_count"]
                    if tel.get("structured_ok"):
                        bucket["structured"] += 1
            provider_list = []
            for name, b in sorted(providers.items()):
                latencies = sorted(b["latencies"])
                p50 = latencies[len(latencies)//2] if latencies else None
                p95 = latencies[int(len(latencies)*0.95)] if latencies else None
                # latency histogram bins: 0-500ms, 500-1000ms, 1-2s, 2-5s, 5-10s, 10s+
                lat_bins = [{"bin": "<500ms", "count": 0}, {"bin": "0.5-1s", "count": 0}, {"bin": "1-2s", "count": 0}, {"bin": "2-5s", "count": 0}, {"bin": "5-10s", "count": 0}, {"bin": ">10s", "count": 0}]
                for lat in latencies:
                    if lat < 500: lat_bins[0]["count"] += 1
                    elif lat < 1000: lat_bins[1]["count"] += 1
                    elif lat < 2000: lat_bins[2]["count"] += 1
                    elif lat < 5000: lat_bins[3]["count"] += 1
                    elif lat < 10000: lat_bins[4]["count"] += 1
                    else: lat_bins[5]["count"] += 1
                provider_list.append({
                    "name": name,
                    "calls": b["calls"],
                    "error_rate": b["errors"] / max(b["calls"], 1),
                    "latency_p50": p50,
                    "latency_p95": p95,
                    "latency_bins": lat_bins,
                    "null_rate": b["nulls"] / max(b["calls"]*2, 1),  # rough: 2 heads per call
                    "noul_rate": b["nouls"] / max(b["calls"]*2, 1),
                    "structured_rate": b["structured"] / max(b["calls"], 1),
                })
            self._json({"providers": provider_list})
            return
        if self.path.startswith("/api/confidence"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            providers = {}
            for r in records:
                for prov, heads in (r.get("heads") or {}).items():
                    bucket = providers.setdefault(prov, [])
                    for h in heads.values():
                        if isinstance(h, dict) and h.get("confidence") is not None:
                            bucket.append(h["confidence"])
            provider_list = []
            for name, confs in sorted(providers.items()):
                bins = [{"bin": "<0.5", "count": 0}, {"bin": "0.5-0.6", "count": 0}, {"bin": "0.6-0.7", "count": 0}, {"bin": "0.7-0.8", "count": 0}, {"bin": "0.8-0.9", "count": 0}, {"bin": "0.9-1.0", "count": 0}]
                fine = [{"bin": f"{(i*0.05):.2f}-{((i+1)*0.05):.2f}", "count": 0} for i in range(20)]
                for c in confs:
                    if c < 0.5: bins[0]["count"] += 1
                    elif c < 0.6: bins[1]["count"] += 1
                    elif c < 0.7: bins[2]["count"] += 1
                    elif c < 0.8: bins[3]["count"] += 1
                    elif c < 0.9: bins[4]["count"] += 1
                    else: bins[5]["count"] += 1
                    idx = min(int(c / 0.05), 19)
                    fine[idx]["count"] += 1
                provider_list.append({"name": name, "bins": bins, "fine_bins": fine})
            self._json({"providers": provider_list})
            return
        if self.path.startswith("/api/modernbert"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            tasks = {}
            for r in records:
                if "modernbert_raw" not in (r.get("providers_used") or []):
                    continue
                heads = r.get("heads", {}).get("modernbert_raw", {})
                for tid, h in heads.items():
                    if not isinstance(h, dict):
                        continue
                    bucket = tasks.setdefault(tid, {"calls": 0, "top2_gaps": [], "norms": [], "labels": {}})
                    bucket["calls"] += 1
                    if h.get("top2_gap") is not None:
                        bucket["top2_gaps"].append(h["top2_gap"])
                    if h.get("embedding_norm") is not None:
                        bucket["norms"].append(h["embedding_norm"])
                    label = h.get("label")
                    if label:
                        bucket["labels"][label] = bucket["labels"].get(label, 0) + 1
            task_list = []
            for name, b in sorted(tasks.items()):
                top_labels = sorted(b["labels"].items(), key=lambda x: x[1], reverse=True)[:5]
                top2 = b["top2_gaps"]
                norms = b["norms"]
                top2_bins = [{"bin": f"{(i*0.1):.1f}-{((i+1)*0.1):.1f}", "count": 0} for i in range(10)]
                for g in top2:
                    idx = min(int(g / 0.1), 9)
                    top2_bins[idx]["count"] += 1
                norm_bins = None
                if norms:
                    lo = min(norms)
                    hi = max(norms)
                    if hi > lo:
                        step = (hi - lo) / 10 or 0.01
                        norm_bins = [{"bin": f"{lo + i*step:.2f}-{lo + (i+1)*step:.2f}", "count": 0} for i in range(10)]
                        for n in norms:
                            idx = min(int((n - lo) / step), 9)
                            norm_bins[idx]["count"] += 1
                task_list.append({
                    "name": name,
                    "calls": b["calls"],
                    "avg_top2": sum(top2)/len(top2) if top2 else None,
                    "avg_norm": sum(norms)/len(norms) if norms else None,
                    "top_labels": [{"label": l, "count": c} for l, c in top_labels],
                    "top2_bins": top2_bins,
                    "norm_bins": norm_bins,
                })
            self._json({"tasks": task_list})
            return
        if self.path.startswith("/api/agreement"):
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            pairs = {}
            for r in records:
                sha = r.get("input_sha256")
                if not sha:
                    continue
                heads = r.get("heads", {})
                provs = [p for p in r.get("providers_used", []) if p in heads]
                for i in range(len(provs)):
                    for j in range(i+1, len(provs)):
                        a, b = provs[i], provs[j]
                        ha, hb = heads.get(a, {}), heads.get(b, {})
                        labels_a = [v.get("label") for v in ha.values() if isinstance(v, dict)]
                        labels_b = [v.get("label") for v in hb.values() if isinstance(v, dict)]
                        key = f"{a} vs {b}"
                        bucket = pairs.setdefault(key, {"compared": 0, "agree": 0})
                        bucket["compared"] += 1
                        if set(labels_a) & set(labels_b):
                            bucket["agree"] += 1
            pair_list = [{"pair": k, "compared": v["compared"], "agree": v["agree"], "rate": v["agree"]/max(v["compared"],1)} for k,v in pairs.items()]
            self._json({"pairs": pair_list})
            return
        if self.path.startswith("/api/calibration"):
            feedback = self._filter_days(self._read_jsonl(Path("feedback") / "feedback.jsonl"))
            records = self._filter_days(self._read_jsonl(self._today_events_path()))
            if not records and self._base.exists():
                all_records = []
                for p in sorted(self._base.rglob("events.jsonl")):
                    all_records.extend(self._read_jsonl(p.relative_to(self._base)))
                records = self._filter_days(all_records)
            index = {}
            for r in records:
                index.setdefault(r.get("input_sha256",""), []).append(r)
            bins = []
            for fb in feedback:
                sha = fb.get("input_sha256")
                task = fb.get("task")
                prov = fb.get("provider")
                true_label = fb.get("label")
                for r in index.get(sha, []):
                    heads = r.get("heads", {})
                    for p, h in heads.items():
                        if prov and p != prov:
                            continue
                        for v in h.values():
                            if isinstance(v, dict) and v.get("confidence") is not None:
                                conf = v["confidence"]
                                correct = 1 if v.get("label") == true_label else 0
                                bin_label = "<0.5" if conf < 0.5 else ("0.5-0.6" if conf < 0.6 else ("0.6-0.7" if conf < 0.7 else ("0.7-0.8" if conf < 0.8 else ("0.8-0.9" if conf < 0.9 else "0.9-1.0"))))
                                bins.append({"provider": p, "bin": bin_label, "correct": correct})
            # aggregate
            agg = {}
            for b in bins:
                key = (b["provider"], b["bin"])
                a = agg.setdefault(key, {"provider": b["provider"], "bin": b["bin"], "samples": 0, "correct": 0})
                a["samples"] += 1
                a["correct"] += b["correct"]
            out = []
            for a in agg.values():
                a["accuracy"] = a["correct"] / max(a["samples"], 1)
                out.append(a)
            self._json({"bins": out})
            return
        self.send_response(404)
        self.end_headers()


def cmd_dashboard(args: argparse.Namespace) -> int:
    """
    Start a local stdlib dashboard for provider telemetry.
    Binds to 127.0.0.1:8765 by default; open / no-open to control browser.
    """
    port = getattr(args, "port", 8765)
    host = getattr(args, "host", "127.0.0.1")
    days = getattr(args, "days", 7)
    no_open = getattr(args, "no_open", False)

    if not shutil.which("python3"):
        print("error: python3 not found", file=sys.stderr)
        return EXIT_ERROR

    import socketserver, threading
    from pathlib import Path

    base = LOG_DIR

    def make_handler(*a, **kw):
        return _DashboardHandler(*a, days=days, base=base, **kw)

    try:
        with socketserver.TCPServer((host, port), make_handler) as httpd:
            url = f"http://{host}:{port}/"
            print(f"dev-decisions dashboard — {url}  (Ctrl-C to stop)")
            if not no_open:
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:
                    pass
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped")
                return EXIT_OK
    except OSError as e:
        print(f"error: dashboard failed to start: {e}", file=sys.stderr)
        return EXIT_ERROR

