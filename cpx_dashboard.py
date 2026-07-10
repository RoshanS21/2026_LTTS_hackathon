#!/usr/bin/env python3
"""
cpx_dashboard.py
================
Piece 5 for the LIVE hardware path: the same live web dashboard as dashboard.py,
but fed by the real Circuit Playground Express sensor over USB serial instead of
a replayed CSV. It closes the whole on-stage loop on real hardware:

  CPX (vibration/temp/sound) --serial--> Pi: detect (cpx_detector's 3-tier core)
    -> summarize (edge LLM + fallback) -> actuate (ESP32-C6 servo / GPIO / mock)
    -> stream to a self-updating page.

Streaming detection, done honestly. A real edge node has no labelled history,
so on start we collect a short BASELINE window of the machine at rest, then
fit the statistical models on it (IsolationForest + a robust CUSUM baseline)
-- the "train offline on healthy history, infer online" split -- and from then
on every incoming frame is scored live (threshold + CUSUM step + IF predict,
a few ms/frame). The fixed safety thresholds need no training and are active
from the first frame.

Button A is the honest manual-confirm trigger (see cpx_detector): a held button
opens AND confirms an event, so the demo still lands if a physical shake/warm
doesn't read on stage -- and the event is labelled a manual inspection request,
never a faked sensor fault.

USAGE
-----
  python3 cpx_dashboard.py                      # live CPX, ESP32 servo (mock if absent)
  python3 cpx_dashboard.py --gpio               # actuate the GPIO17 LED instead of the servo
  python3 cpx_dashboard.py --mock --fault-at 8 --fault-duration 4   # no hardware, synthetic
  python3 cpx_dashboard.py --no-open            # headless (no browser tab)
"""

import argparse
import json
import queue
import statistics
import threading
import time
import webbrowser
from datetime import datetime
from types import SimpleNamespace

import numpy as np
from flask import Flask, jsonify, render_template_string
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

import cpx_detector
import cpx_serial_reader
from anomaly_detector import CusumDetector, cusum_baseline
from benchmark_edge_llm import DEFAULT_HOST
from cpx_detector import (
    CPX_LABELS, FEATURE_COLUMNS, GRAVITY, SOUND_MAX, TEMP_C_MAX,
    VIBRATION_DEV_MAX, infer_trigger,
)
from esp32_actuator import ServoActuator
from edge_actuator import ALARM_PIN, Actuator
from llm_summary import DEFAULT_MODEL, DEFAULT_TIMEOUT, summarize_event, warm_up

app = Flask(__name__)

HISTORY_LEN = 150   # sparkline points sent to the client

# The CPX summary profile: reuse cpx_detector's prompt + fallback so the LLM
# layer describes vibration/thermal/acoustic faults, not J1939 engine faults.
CPX_PROFILE = SimpleNamespace(
    format_anomaly_prompt=cpx_detector.format_anomaly_prompt,
    fallback_summary=cpx_detector.fallback_summary,
)

STATE = {
    "status": "starting",  # starting | connecting | baseline | warming | streaming | error
    "asset_id": None,
    "frames": 0,
    "mode": None,          # "live" | "mock"
    "events": [],
    "live": None,          # {"ts", "signals", "breaches", "history", "button"}
    "button": False,
    "stats": {},
    "actions": [],
    "alarm_active": False,
    "display_offset": 0,   # events before this index are hidden (Clear button)
    "version": 0,
}
STATE_LOCK = threading.Lock()


def signal_breaches(signals):
    """CPX hard-bound breaches: vibration is a two-sided deviation from gravity;
    temp and sound are one-sided highs."""
    return {
        "accel_mag_ms2": abs(signals["accel_mag_ms2"] - GRAVITY) > VIBRATION_DEV_MAX,
        "temp_c": signals["temp_c"] > TEMP_C_MAX,
        "sound_level": signals["sound_level"] > SOUND_MAX,
    }


def _mutate(fn):
    with STATE_LOCK:
        fn(STATE)
        STATE["version"] += 1


class SummaryWorker:
    """Single background LLM worker (one small model, one Pi), profile-aware so
    it can summarize CPX events. Never stalls the frame loop."""

    def __init__(self, host, model, timeout, profile):
        self.host, self.model, self.timeout = host, model, timeout
        self.profile = profile
        self.queue = queue.Queue()
        self.latencies = []
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, state_idx, event):
        def set_pending(s):
            s["events"][state_idx]["status"] = "summarizing"
        _mutate(set_pending)
        self.queue.put((state_idx, event))

    def _run(self):
        while True:
            state_idx, event = self.queue.get()
            result = summarize_event(self.host, self.model, event, self.timeout,
                                     self.profile)
            if result["source"] == "llm":
                self.latencies.append(result["latency_s"])
            uplink = len(json.dumps({**result, "event": event}).encode("utf-8"))

            def apply(s):
                slot = s["events"][state_idx]
                slot.update(status="done", source=result["source"],
                            text=result["text"], latency_s=result["latency_s"])
                s["stats"]["uplink_bytes"] += uplink
                s["stats"]["summaries_done"] += 1
                if self.latencies:
                    s["stats"]["llm_median_s"] = round(
                        statistics.median(self.latencies), 2)
            _mutate(apply)
            self.queue.task_done()


def frame_source(args):
    """Yield (byte_len, frame_dict) from the CPX -- live serial or synthetic
    mock -- reusing cpx_serial_reader's parser and mock so the wire format
    can't drift from the real reader."""
    if args.mock:
        _mutate(lambda s: s.update(mode="mock"))
        for line in cpx_serial_reader.mock_lines(10.0, args.fault_at, args.fault_duration):
            frame = cpx_serial_reader.parse_line(line)
            if frame is not None:
                yield len(line) + 1, frame
    else:
        _mutate(lambda s: s.update(mode="live"))
        port = args.port or cpx_serial_reader.find_cpx_port()
        if port is None:
            raise RuntimeError("no CPX serial port found (plug it in, pass --port, "
                               "or use --mock)")
        ser = cpx_serial_reader.open_serial(port)
        print(f"CPX connected on {port}", flush=True)
        for raw in ser:
            frame = cpx_serial_reader.parse_line(raw.decode("utf-8", "replace"))
            if frame is not None:
                yield len(raw), frame


def feat_vec(frame):
    return np.array([float(frame[c]) for c in FEATURE_COLUMNS])


def run_pipeline(args, actuator):
    _mutate(lambda s: s.update(status="connecting"))
    src = frame_source(args)

    # Warm the LLM in parallel with baseline collection so the first summary
    # doesn't pay the cold-load cost on stage.
    threading.Thread(target=warm_up, args=(args.host, args.model), daemon=True).start()

    history = {c: [] for c in FEATURE_COLUMNS}
    raw_bytes = 0
    n_baseline = max(20, int(args.baseline_seconds * 10))  # CPX streams ~10 Hz

    def push_live(frame, byte_len):
        nonlocal raw_bytes
        raw_bytes += byte_len
        signals = {c: float(frame[c]) for c in FEATURE_COLUMNS}
        for c in FEATURE_COLUMNS:
            history[c].append(round(signals[c], 2))
            if len(history[c]) > HISTORY_LEN:
                history[c].pop(0)
        live = {"ts": frame["timestamp"], "signals": signals,
                "breaches": signal_breaches(signals),
                "history": {c: list(history[c]) for c in FEATURE_COLUMNS}}
        button = int(frame.get("button_a", 0)) == 1

        def upd(s):
            s["frames"] += 1
            s["live"] = live
            s["button"] = button
            s["asset_id"] = frame.get("asset_id")
            s["stats"]["raw_bytes"] = raw_bytes
            s["actions"] = actuator.log_entries()
            s["alarm_active"] = actuator.alarm_active
        _mutate(upd)
        return signals, button

    # --- init stats + collect the baseline window (machine at rest) ---
    _mutate(lambda s: s["stats"].update({
        "model": args.model, "llm_median_s": None,
        "raw_bytes": 0, "uplink_bytes": 0, "summaries_done": 0,
        "confirmed_events": 0, "monitor_events": 0, "debounced": 0,
        "actuator_backend": actuator.backend, "actuator_kind": args.actuator_kind,
        "baseline_frames": n_baseline,
    }))
    _mutate(lambda s: s.update(status="baseline"))

    baseline = []
    for byte_len, frame in src:
        push_live(frame, byte_len)
        baseline.append(feat_vec(frame))
        if len(baseline) >= n_baseline:
            break

    base = np.array(baseline)
    scaler = StandardScaler().fit(base)
    iforest = IsolationForest(n_estimators=200, contamination=args.contamination,
                              random_state=42).fit(scaler.transform(base))
    med, mad = cusum_baseline(base)
    cusum = CusumDetector(med, mad, h=args.cusum_h)
    base_med, base_mad = med, mad  # for peak-severity snapshotting

    worker = SummaryWorker(args.host, args.model, args.timeout, CPX_PROFILE)
    _mutate(lambda s: s.update(status="streaming"))

    max_event_frames = int(args.max_event_seconds * 10)  # bound on one event
    open_event = None

    def close_event():
        peak = open_event["peak"]
        confirmed = open_event["confirmed"]
        lead_frames = (open_event["first_confirm_i"] - open_event["start_i"]
                       if confirmed and open_event["first_confirm_i"] is not None
                       else None)
        event = {
            "start_ts": open_event["start_ts"],
            "end_ts": open_event["last_ts"],
            "asset_id": open_event["asset_id"],
            "duration_s": round((datetime.fromisoformat(open_event["last_ts"])
                                 - datetime.fromisoformat(open_event["start_ts"])
                                 ).total_seconds(), 1),
            "confirmed": confirmed,
            "manual_trigger": open_event["manual"],
            "confirm_lead_s": (round(lead_frames / 10.0, 1)
                               if lead_frames else None),
            "signals": peak["signals"],
        }
        event["trigger"] = infer_trigger(event)
        idx = open_event["state_idx"]

        def apply(s):
            slot = s["events"][idx]
            slot["event"] = event
            slot["breaches"] = signal_breaches(event["signals"])
            # Don't count an event that Clear has already hidden (it was in
            # progress when the feed was cleared), so the tile matches the feed.
            if idx >= s["display_offset"]:
                if confirmed:
                    s["stats"]["confirmed_events"] += 1
                else:
                    s["stats"]["monitor_events"] += 1
        _mutate(apply)
        worker.submit(idx, event)
        actuator.alarm_off()

    i = n_baseline
    for byte_len, frame in src:
        if args.duration and i >= n_baseline + int(args.duration * 10):
            break
        signals, button = push_live(frame, byte_len)
        v = feat_vec(frame)

        # --- live per-frame detection (the real edge inference step) ---
        t_hit = bool(cpx_detector.threshold_flags_cpx(v.reshape(1, -1))[0])
        cusum_hit = cusum.step(v)
        if_hit = bool(iforest.predict(scaler.transform(v.reshape(1, -1)))[0] == -1)
        flagged = t_hit or cusum_hit or if_hit or button
        confirm = t_hit or button          # hard breach or manual press = confirmed
        severity = float(np.max(np.abs((v - base_med) / base_mad)))

        if flagged:
            if open_event is None:
                open_event = {
                    "start_i": i, "start_ts": frame["timestamp"],
                    "last_i": i, "last_ts": frame["timestamp"],
                    "flag_count": 0, "confirmed": False, "manual": False,
                    "first_confirm_i": None, "promoted": False, "state_idx": None,
                    "actuated": False, "asset_id": frame.get("asset_id"),
                    "peak": {"sev": -1.0, "signals": signals},
                }
            oe = open_event
            oe["last_i"], oe["last_ts"] = i, frame["timestamp"]
            oe["flag_count"] += 1
            oe["manual"] = oe["manual"] or button
            if confirm and oe["first_confirm_i"] is None:
                oe["first_confirm_i"] = i
            oe["confirmed"] = oe["confirmed"] or confirm
            if severity > oe["peak"]["sev"]:
                oe["peak"] = {"sev": severity, "signals": signals}

            # Confirmed events (hard breach / Button A) always promote.
            # Statistical-only runs promote to a "monitor" event only if they
            # sustain past the evidence floor -- unless --confirmed-only, which
            # suppresses monitor cards entirely for a hard-fault-only feed.
            statistical_ok = (not args.confirmed_only
                              and oe["flag_count"] >= args.min_unconfirmed)
            if not oe["promoted"] and (oe["confirmed"] or statistical_ok):
                oe["promoted"] = True
                oe["state_idx"] = len(STATE["events"])
                snap = {
                    "start_ts": oe["start_ts"], "end_ts": frame["timestamp"],
                    "asset_id": oe["asset_id"],
                    "duration_s": round((i - oe["start_i"]) / 10.0, 1),
                    "confirmed": oe["confirmed"], "manual_trigger": oe["manual"],
                    "confirm_lead_s": None, "signals": oe["peak"]["signals"],
                }
                snap["trigger"] = infer_trigger(snap)

                def open_card(s):
                    s["events"].append({
                        "event": snap, "breaches": signal_breaches(snap["signals"]),
                        "status": "active", "source": None, "text": None,
                        "latency_s": None,
                    })
                _mutate(open_card)
                actuator.handle_event(snap, trigger=snap["trigger"])
                oe["actuated"] = oe["confirmed"]
            elif oe["promoted"] and confirm and not oe["actuated"]:
                idx = oe["state_idx"]
                lead = round((i - oe["start_i"]) / 10.0, 1)

                def upgrade(s):
                    ev = s["events"][idx]["event"]
                    ev["confirmed"] = True
                    ev["confirm_lead_s"] = lead
                _mutate(upgrade)
                actuator.handle_event({"confirmed": True, "asset_id": oe["asset_id"]},
                                      trigger="sensor")
                oe["actuated"] = True

            if oe["promoted"]:
                idx = oe["state_idx"]

                def extend(s):
                    ev = s["events"][idx]["event"]
                    ev["end_ts"] = frame["timestamp"]
                    ev["duration_s"] = round((i - oe["start_i"]) / 10.0, 1)
                _mutate(extend)

                # A persistent signal offset (e.g. a warmed board that cools
                # slowly) keeps the statistical detectors flagging with no clear
                # gap, so an event would never close. Bound it: close + summarize
                # + release the actuator, then let it re-open if still flagging.
                if (i - oe["start_i"]) >= max_event_frames:
                    close_event()
                    open_event = None
        elif open_event is not None and (i - open_event["last_i"] > args.merge_gap):
            if open_event["promoted"]:
                close_event()
            else:
                _mutate(lambda s: s["stats"].__setitem__(
                    "debounced", s["stats"]["debounced"] + 1))
            open_event = None
        i += 1

    if open_event is not None and open_event["promoted"]:
        close_event()
    worker.queue.join()
    _mutate(lambda s: s.update(status="done"))


PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edge Maintenance Feed — Live CPX</title>
<style>
  body { font-family: -apple-system, Roboto, sans-serif; background: #0f172a;
         color: #e2e8f0; margin: 0; padding: 16px; }
  h1 { font-size: 1.2rem; margin: 0 0 2px; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em;
       color: #64748b; margin: 20px 0 8px; }
  #clearBtn { float: right; text-transform: none; letter-spacing: normal;
              font-size: 0.72rem; font-weight: 600; color: #cbd5e1;
              background: #334155; border: none; border-radius: 6px;
              padding: 3px 10px; cursor: pointer; }
  #clearBtn:hover { background: #475569; }
  #sub { color: #94a3b8; font-size: 0.85rem; margin-bottom: 12px; }
  #status { display: inline-block; padding: 3px 10px; border-radius: 999px;
            font-size: 0.75rem; font-weight: 600; }
  .pill { display: none; padding: 3px 10px; border-radius: 999px;
          font-size: 0.75rem; font-weight: 700; color: white; margin-left: 8px; }
  #alarm { background: #dc2626; animation: pulse 0.8s infinite; }
  #manual { background: #7c3aed; }
  @keyframes pulse { 50% { opacity: 0.45; } }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
           gap: 10px; }
  .tile { background: #1e293b; border-radius: 10px; padding: 10px 14px; }
  .tile .k { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em;
             color: #64748b; margin-bottom: 4px; }
  .tile .v { font-size: 1.25rem; font-weight: 700; color: #f1f5f9; }
  .tile .d { font-size: 0.72rem; color: #94a3b8; margin-top: 2px; }
  .sigs { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 10px; }
  .sig { background: #1e293b; border-radius: 10px; padding: 10px 14px; }
  .sig .name { font-size: 0.72rem; color: #94a3b8; }
  .sig .val { font-size: 1.15rem; font-weight: 700; margin: 2px 0 6px; }
  .sig .val.bad { color: #f87171; }
  .sig canvas { width: 100%; height: 44px; display: block; }
  .card { background: #1e293b; border-radius: 10px; padding: 14px 16px;
          margin-bottom: 12px; border-left: 4px solid #334155; }
  .card.active { border-left-color: #dc2626; animation: pulse 1.2s infinite; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
           font-size: 0.72rem; font-weight: 600; color: white; margin-bottom: 8px; }
  .signals { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 16px;
             font-size: 0.82rem; color: #cbd5e1; margin: 8px 0; }
  .signals .bad { color: #f87171; font-weight: 700; }
  .summary { font-size: 0.95rem; line-height: 1.4; margin-top: 6px; }
  .meta { font-size: 0.72rem; color: #64748b; margin-top: 8px; }
  #empty { color: #64748b; font-style: italic; }
  #actions { background: #1e293b; border-radius: 10px; padding: 10px 14px;
             font-family: ui-monospace, monospace; font-size: 0.75rem;
             color: #cbd5e1; max-height: 160px; overflow-y: auto; }
  #actions .a-ALARM_ON { color: #f87171; font-weight: 700; }
  #actions .a-ALARM_OFF { color: #4ade80; }
  #actions .a-MONITOR { color: #94a3b8; }
  #actions .a-C6 { color: #38bdf8; }
</style>
</head>
<body>
  <h1>Edge Maintenance Feed — Live CPX Sensor</h1>
  <div id="sub">asset: --</div>
  <div><span id="status">starting</span>
       <span class="pill" id="alarm">ACTUATOR ENGAGED</span>
       <span class="pill" id="manual">MANUAL TRIGGER</span></div>

  <h2>Run stats</h2>
  <div class="tiles" id="tiles"></div>

  <h2>Live sensor (on-device detection, per frame)</h2>
  <div class="sigs" id="sigs"></div>

  <h2>Detected events &rarr; edge-LLM summaries
      <button id="clearBtn" onclick="clearFeed()">clear feed</button></h2>
  <div id="events"><div id="empty">waiting for the sensor stream...</div></div>

  <h2>Perceive &rarr; decide &rarr; act (actuator log)</h2>
  <div id="actions">no actions yet</div>

<script>
let lastVersion = -1;
const STATUS_COLORS = {starting:'#475569', connecting:'#475569', baseline:'#0891b2',
                       warming:'#7c3aed', streaming:'#2563eb', done:'#16a34a', error:'#dc2626'};
// [label, unit, bound, kind]; vibration bound is the upper gravity+dev line.
const SIG_LABELS = {
  accel_mag_ms2: ['Vibration |accel|', 'm/s\\u00b2', 17.8, 'over'],
  temp_c: ['Temperature', '\\u00b0C', 30, 'over'],
  sound_level: ['Acoustic (mic RMS)', '', 400, 'over'],
};
const SIG_ORDER = Object.keys(SIG_LABELS);

function fmtBytes(b) {
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b >= 1024) return (b / 1024).toFixed(1) + ' KB';
  return (b || 0) + ' B';
}

function renderTiles(d) {
  const st = d.stats || {};
  const saved = st.raw_bytes > 0
    ? (100 * (1 - st.uplink_bytes / st.raw_bytes)).toFixed(1) + '%' : '--';
  const tiles = [
    ['Events detected',
     (st.confirmed_events ?? 0) + ' confirmed',
     (st.monitor_events ?? 0) + ' monitor \\u00b7 ' + (st.debounced ?? 0) + ' noise blips debounced'],
    ['Edge LLM latency',
     st.llm_median_s != null ? st.llm_median_s.toFixed(2) + ' s' : '--',
     'median per summary \\u00b7 ' + (st.model || '') + ' on-Pi'],
    ['Bandwidth saved', saved,
     fmtBytes(st.uplink_bytes) + ' uplinked vs ' + fmtBytes(st.raw_bytes) + ' raw sensor'],
    ['Actuator', d.alarm_active ? 'ENGAGED' : 'armed',
     (st.actuator_kind || '--') + ' \\u00b7 ' + (st.actuator_backend || '--') + ' backend'],
  ];
  document.getElementById('tiles').innerHTML = tiles.map(([k, v, dd]) =>
    `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div>
     <div class="d">${dd}</div></div>`).join('');
}

function drawSpark(canvas, values, bound, bad) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !values || values.length < 2) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (bound != null) { lo = Math.min(lo, bound); hi = Math.max(hi, bound); }
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const x = i => i / (values.length - 1) * w;
  const y = v => h - (v - lo) / (hi - lo) * h;
  if (bound != null) {
    ctx.strokeStyle = '#7f1d1d'; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y(bound)); ctx.lineTo(w, y(bound)); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.strokeStyle = '#60a5fa'; ctx.lineWidth = 2; ctx.lineJoin = 'round';
  ctx.beginPath();
  values.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
  ctx.stroke();
  const last = values[values.length - 1];
  ctx.fillStyle = bad ? '#f87171' : '#e2e8f0';
  ctx.beginPath(); ctx.arc(w - 2, y(last), 3, 0, 7); ctx.fill();
}

function renderSigs(d) {
  const container = document.getElementById('sigs');
  if (!d.live) { container.innerHTML = '<div class="sig">no data yet</div>'; return; }
  if (container.children.length !== SIG_ORDER.length) {
    container.innerHTML = SIG_ORDER.map(() =>
      `<div class="sig"><div class="name"></div><div class="val"></div><canvas></canvas></div>`).join('');
  }
  SIG_ORDER.forEach((c, i) => {
    const [label, unit, bound] = SIG_LABELS[c];
    const el = container.children[i];
    const bad = d.live.breaches[c];
    el.querySelector('.name').textContent = label + (bound != null ? ` (limit ${bound}${unit})` : '');
    const val = el.querySelector('.val');
    val.textContent = d.live.signals[c].toFixed(1) + (unit ? ' ' + unit : '');
    val.className = 'val' + (bad ? ' bad' : '');
    drawSpark(el.querySelector('canvas'), d.live.history[c], bound, bad);
  });
}

function renderEvents(d) {
  const container = document.getElementById('events');
  if (!d.events.length) {
    container.innerHTML = '<div id="empty">no anomalies yet \\u2014 shake, warm, or press Button A</div>';
    return;
  }
  const labelMap = {llm: ['AI Diagnosis', '#2563eb'], fallback: ['Template (offline)', '#d97706'],
                    monitor: ['Monitoring \\u2013 no confirmed fault', '#6b7280']};
  let html = '';
  for (const r of [...d.events].reverse()) {
    let label, color, extraCls = '';
    if (r.status === 'active') { [label, color] = ['Anomaly in progress', '#dc2626']; extraCls = ' active'; }
    else if (r.status === 'summarizing') { [label, color] = ['Summarizing on-device\\u2026', '#7c3aed']; }
    else { [label, color] = labelMap[r.source] || [r.source, '#334155']; }
    const trig = r.event.trigger ? r.event.trigger.replace('_', ' ') : '';
    const manual = r.event.manual_trigger ? ' <span style="color:#a78bfa">[manual]</span>' : '';
    const sig = SIG_ORDER.map(c => {
      const [lab, unit] = SIG_LABELS[c];
      const cls = r.breaches[c] ? 'bad' : '';
      return `<div class="${cls}">${lab}: ${r.event.signals[c].toFixed(1)}${unit ? ' ' + unit : ''}</div>`;
    }).join('');
    html += `<div class="card${extraCls}" style="border-left-color:${color}">
      <span class="badge" style="background:${color}">${label}</span>
      <span style="font-size:.72rem;color:#94a3b8"> ${trig}${manual}</span>
      <div class="signals">${sig}</div>
      ${r.text ? `<div class="summary">${r.text}</div>` : ''}
      <div class="meta">${r.event.start_ts.slice(11,19)} \\u2192 ${r.event.end_ts.slice(11,19)}
        &middot; ${r.event.duration_s}s
        ${r.event.confirm_lead_s > 0 ? '&middot; <b>flagged ' + r.event.confirm_lead_s + 's before hard breach</b>' : ''}
        ${r.latency_s != null ? '&middot; summary in ' + r.latency_s.toFixed(2) + 's' : ''}</div>
    </div>`;
  }
  container.innerHTML = html;
}

function renderActions(d) {
  const el = document.getElementById('actions');
  if (!d.actions || !d.actions.length) { el.textContent = 'no actions yet'; return; }
  el.innerHTML = [...d.actions].reverse().map(a =>
    `<div class="a-${a.action}">${a.ts.slice(11, 19)}  ${(a.action||'').padEnd(9)} ${a.detail}</div>`).join('');
}

function render(d) {
  const st = d.stats || {};
  let sub = `asset: ${d.asset_id || '--'} \\u00b7 ${d.mode || ''} \\u00b7 ${d.frames} frames`;
  if (d.status === 'baseline') sub += ` \\u00b7 learning baseline (keep sensor at rest)`;
  document.getElementById('sub').textContent = sub;
  const s = document.getElementById('status');
  s.textContent = d.status; s.style.background = STATUS_COLORS[d.status] || '#475569';
  document.getElementById('alarm').style.display = d.alarm_active ? 'inline-block' : 'none';
  document.getElementById('manual').style.display = d.button ? 'inline-block' : 'none';
  renderTiles(d); renderSigs(d); renderEvents(d); renderActions(d);
}

async function clearFeed() {
  try { await fetch('/api/clear', {method: 'POST'}); lastVersion = -1; } catch (e) {}
}

async function poll() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    if (data.version !== lastVersion) { lastVersion = data.version; render(data); }
  } catch (e) { /* keep polling */ }
  setTimeout(poll, 400);
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
        # The pipeline thread indexes events by absolute position, so we never
        # mutate the list on Clear -- we just hide everything before the offset.
        resp = dict(STATE)
        resp["events"] = STATE["events"][STATE["display_offset"]:]
    return jsonify(resp)


@app.route("/api/clear", methods=["POST"])
def api_clear():
    """Clear the visible event feed (the summaries) without disturbing the live
    stream: hide current events and reset the visible event counters. The
    bandwidth ledger and LLM-latency stats stay cumulative for the session."""
    def clr(s):
        s["display_offset"] = len(s["events"])
        s["stats"]["confirmed_events"] = 0
        s["stats"]["monitor_events"] = 0
        s["stats"]["debounced"] = 0
    _mutate(clr)
    return jsonify({"ok": True})


def main():
    ap = argparse.ArgumentParser(description="Live CPX hardware dashboard for the LTTS edge-AI demo.")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=25.0,
                    help="Edge-LLM timeout. Higher than the J1939 default because a "
                         "sustained fault queues several summaries under CPU contention; "
                         "more headroom means a real AI diagnosis instead of the template.")
    ap.add_argument("--contamination", type=float, default=0.05,
                    help="IsolationForest outlier fraction; lower = fewer monitor events")
    ap.add_argument("--cusum-h", type=float, default=12.0,
                    help="CUSUM decision threshold; higher = less drift-sensitive (fewer monitors)")
    ap.add_argument("--confirmed-only", action="store_true",
                    help="Suppress statistical 'monitor' events entirely; only hard-breach / "
                         "Button A events appear (cleanest feed for a busy demo)")
    ap.add_argument("--baseline-seconds", type=float, default=8.0,
                    help="Seconds of at-rest sensor data to train the statistical models on")
    # The CPX streams ~10 Hz (vs the J1939 path's 1 Hz), so evidence is counted
    # in frames that are 10x shorter in time -- these floors are scaled up to
    # match: ~1.2 s of bridging and ~1.8 s of sustained statistical evidence,
    # so one shake reads as one event and sub-second jitter is debounced rather
    # than spawning a monitor card per twitch.
    ap.add_argument("--min-unconfirmed", type=int, default=30,
                    help="Evidence floor (flagged frames, ~10 Hz => ~3 s) before a "
                         "statistical-only event; higher = fewer monitor events")
    ap.add_argument("--merge-gap", type=int, default=12,
                    help="Hysteresis: clear frames (~10 Hz) tolerated inside one event")
    ap.add_argument("--max-event-seconds", type=float, default=15.0,
                    help="Force-close (and summarize) an event after this long, so a "
                         "persistent offset can't hold one event open forever")
    ap.add_argument("--port", default=None, help="CPX serial port (default: auto-detect)")
    ap.add_argument("--mock", action="store_true", help="Synthetic frames, no hardware")
    ap.add_argument("--fault-at", type=float, default=None, help="Mock: inject a shake burst at t=N s")
    ap.add_argument("--fault-duration", type=float, default=3.0, help="Mock: burst duration (s)")
    ap.add_argument("--duration", type=float, default=None, help="Stop after N s of streaming (after baseline)")
    ap.add_argument("--http-port", type=int, default=5000)
    ap.add_argument("--gpio", action="store_true",
                    help="Actuate the GPIO17 LED (edge_actuator) instead of the ESP32 servo")
    ap.add_argument("--esp-port", default=None, help="ESP32-C6 serial port (default: auto-detect)")
    ap.add_argument("--no-serial", action="store_true", help="Force the servo mock backend")
    ap.add_argument("--pin", type=int, default=ALARM_PIN, help="GPIO pin for --gpio")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if args.gpio:
        actuator = Actuator(pin=args.pin, use_gpio=True)
        args.actuator_kind = f"GPIO{args.pin} LED"
    else:
        actuator = ServoActuator(port=args.esp_port, use_serial=not args.no_serial)
        args.actuator_kind = "ESP32-C6 servo"

    def pipeline():
        try:
            run_pipeline(args, actuator)
        except Exception as e:  # noqa: BLE001 -- surface any crash on the page
            print(f"pipeline error: {e}", flush=True)
            _mutate(lambda s: s.update(status="error", asset_id=f"error: {e}"))

    threading.Thread(target=pipeline, daemon=True).start()

    if not args.no_open:
        url = f"http://127.0.0.1:{args.http_port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        app.run(host="0.0.0.0", port=args.http_port, debug=False)
    finally:
        actuator.close()


if __name__ == "__main__":
    main()
