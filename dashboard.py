#!/usr/bin/env python3
"""
dashboard.py
=============
Piece 5 of the LTTS edge-AI demo (NFC-free version): a single Flask page that
plays the whole edge pipeline -- piece 2's CSV -> piece 3's detector -> piece
4's LLM/fallback summary -- as a live feed. On start it launches its own
pipeline run in a background thread and opens a browser tab pointed at
itself, so there's nothing to click or type once it's running: events and
their summaries populate onto the page as they're produced, the same way
they would in a real streaming edge deployment.

USAGE
-----
  python3 dashboard.py
  python3 dashboard.py --csv data/demo_run.csv --model qwen2.5:1.5b --no-open
"""

import argparse
import threading
import time
import webbrowser

import numpy as np
from flask import Flask, jsonify, render_template_string

from anomaly_detector import (
    COOLANT_C_MAX, FEATURE_COLUMNS, OIL_KPA_MIN, RPM_MAX, find_events,
    isoforest_flags, load_csv, threshold_flags,
)
from benchmark_edge_llm import DEFAULT_HOST
from llm_summary import DEFAULT_MODEL, DEFAULT_TIMEOUT, summarize_event, warm_up

app = Flask(__name__)

STATE = {
    "status": "starting",  # starting | warming | running | done | error
    "asset_id": None,
    "events": [],
    "version": 0,
}
STATE_LOCK = threading.Lock()

BREACH_BOUNDS = {
    "spn190_engine_speed_rpm": ("over", RPM_MAX),
    "spn110_coolant_temp_c": ("over", COOLANT_C_MAX),
    "spn100_oil_pressure_kpa": ("under", OIL_KPA_MIN),
    "spn175_oil_temp_c": (None, None),
}


def signal_breaches(signals):
    breaches = {}
    for col, value in signals.items():
        kind, bound = BREACH_BOUNDS.get(col, (None, None))
        if kind == "over":
            breaches[col] = value > bound
        elif kind == "under":
            breaches[col] = value < bound
        else:
            breaches[col] = False
    return breaches


def _set_status(status):
    with STATE_LOCK:
        STATE["status"] = status
        STATE["version"] += 1


def _push_event(result):
    result["breaches"] = signal_breaches(result["event"]["signals"])
    with STATE_LOCK:
        STATE["events"].append(result)
        STATE["asset_id"] = result["event"]["asset_id"]
        STATE["version"] += 1


def run_pipeline(csv_path, host, model, timeout, contamination):
    with STATE_LOCK:
        STATE["events"] = []
        STATE["version"] += 1

    rows = load_csv(csv_path)
    features = np.array([[float(r[c]) for c in FEATURE_COLUMNS] for r in rows])
    t_flags = threshold_flags(features)
    if_flags = isoforest_flags(features, contamination=contamination)
    combined = t_flags | if_flags
    events = find_events(rows, features, combined, confirm_flags=t_flags)

    _set_status("warming")
    warm_up(host, model)

    _set_status("running")
    for event in events:
        result = summarize_event(host, model, event, timeout)
        result["event"] = event
        _push_event(result)
        time.sleep(0.4)

    _set_status("done")


SOURCE_LABELS = {
    "llm": ("AI Diagnosis", "#2563eb"),
    "fallback": ("Template (offline)", "#d97706"),
    "monitor": ("Monitoring – no confirmed fault", "#6b7280"),
}

SIGNAL_LABELS = {
    "spn190_engine_speed_rpm": ("Engine Speed", "rpm"),
    "spn110_coolant_temp_c": ("Coolant Temp", "°C"),
    "spn100_oil_pressure_kpa": ("Oil Pressure", "kPa"),
    "spn175_oil_temp_c": ("Oil Temp", "°C"),
}

PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edge Maintenance Feed</title>
<style>
  body { font-family: -apple-system, Roboto, sans-serif; background: #0f172a;
         color: #e2e8f0; margin: 0; padding: 16px; }
  h1 { font-size: 1.2rem; margin: 0 0 4px; }
  #asset { color: #94a3b8; font-size: 0.9rem; margin-bottom: 16px; }
  #status { display: inline-block; padding: 3px 10px; border-radius: 999px;
            font-size: 0.75rem; font-weight: 600; margin-bottom: 16px; }
  .card { background: #1e293b; border-radius: 10px; padding: 14px 16px;
          margin-bottom: 12px; border-left: 4px solid #334155; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
           font-size: 0.72rem; font-weight: 600; color: white; margin-bottom: 8px; }
  .signals { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 16px;
             font-size: 0.82rem; color: #cbd5e1; margin: 8px 0; }
  .signals .bad { color: #f87171; font-weight: 700; }
  .summary { font-size: 0.95rem; line-height: 1.4; margin-top: 6px; }
  .meta { font-size: 0.72rem; color: #64748b; margin-top: 8px; }
  #empty { color: #64748b; font-style: italic; }
</style>
</head>
<body>
  <h1>Edge Maintenance Feed</h1>
  <div id="asset">asset: --</div>
  <div id="status">starting</div>
  <div id="events"><div id="empty">waiting for the on-device pipeline to start...</div></div>

<script>
let lastVersion = -1;
const STATUS_COLORS = {starting:'#475569', warming:'#7c3aed', running:'#2563eb',
                        done:'#16a34a', error:'#dc2626'};

function fmtSignals(signals, breaches) {
  const labels = {
    spn190_engine_speed_rpm: ['Engine Speed', 'rpm'],
    spn110_coolant_temp_c: ['Coolant Temp', '\\u00b0C'],
    spn100_oil_pressure_kpa: ['Oil Pressure', 'kPa'],
    spn175_oil_temp_c: ['Oil Temp', '\\u00b0C'],
  };
  let html = '';
  for (const col in labels) {
    const [label, unit] = labels[col];
    const cls = breaches[col] ? 'bad' : '';
    html += `<div class="${cls}">${label}: ${signals[col].toFixed(0)} ${unit}</div>`;
  }
  return html;
}

function render(data) {
  document.getElementById('asset').textContent = 'asset: ' + (data.asset_id || '--');
  const statusEl = document.getElementById('status');
  statusEl.textContent = data.status;
  statusEl.style.background = STATUS_COLORS[data.status] || '#475569';

  const container = document.getElementById('events');
  if (data.events.length === 0) {
    container.innerHTML = '<div id="empty">waiting for the on-device pipeline to start...</div>';
    return;
  }
  const labelMap = {llm: ['AI Diagnosis', '#2563eb'], fallback: ['Template (offline)', '#d97706'],
                     monitor: ['Monitoring \\u2013 no confirmed fault', '#6b7280']};
  let html = '';
  for (const r of [...data.events].reverse()) {
    const [label, color] = labelMap[r.source] || [r.source, '#334155'];
    html += `<div class="card" style="border-left-color:${color}">
      <span class="badge" style="background:${color}">${label}</span>
      <div class="signals">${fmtSignals(r.event.signals, r.breaches)}</div>
      <div class="summary">${r.text}</div>
      <div class="meta">${r.event.start_ts} \\u2192 ${r.event.end_ts}
        &middot; ${r.latency_s.toFixed(2)}s</div>
    </div>`;
  }
  container.innerHTML = html;
}

async function poll() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    if (data.version !== lastVersion) {
      lastVersion = data.version;
      render(data);
    }
  } catch (e) { /* keep polling even if a request drops */ }
  setTimeout(poll, 1200);
}
poll();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/state")
def api_state():
    with STATE_LOCK:
        return jsonify(STATE)


def main():
    ap = argparse.ArgumentParser(description="Live dashboard for the LTTS edge-AI demo.")
    ap.add_argument("--csv", default="data/demo_run.csv")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--contamination", type=float, default=0.08)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    threading.Thread(
        target=run_pipeline,
        args=(args.csv, args.host, args.model, args.timeout, args.contamination),
        daemon=True,
    ).start()

    if not args.no_open:
        url = f"http://127.0.0.1:{args.port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
