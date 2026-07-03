#!/usr/bin/env python3
"""
llm_summary.py
===============
Piece 4 of the LTTS edge-AI demo: turn anomaly events (piece 3's output) into
a 2-sentence mechanical maintenance summary via a local Ollama model, with a
deterministic templated fallback if the model errors or misses the demo's
latency budget.

This is deliberately the one part of the pipeline allowed to fail: anomaly
detection (piece 3) is the source of truth, this layer only adds a
natural-language gloss on top of it. If it's slow or down, the fallback
template still gives a correct, if blunter, maintenance summary -- the demo
never has a "silent" failure mode.

USAGE
-----
  python3 llm_summary.py                              # detect + summarize data/demo_run.csv
  python3 llm_summary.py --events-in data/detected_events.json
  python3 llm_summary.py --model gemma2:2b --timeout 8
"""

import argparse
import json
import sys
import time
import urllib.request

import numpy as np

from anomaly_detector import (
    COOLANT_C_MAX, FEATURE_COLUMNS, OIL_KPA_MIN, RPM_MAX, find_events,
    format_anomaly_prompt, isoforest_flags, load_csv, threshold_flags,
)
from benchmark_edge_llm import DEFAULT_HOST, SYSTEM_PROMPT

NUM_PREDICT = 80
DEFAULT_MODEL = "qwen2.5:1.5b"  # Option A pick from benchmark_edge_llm.py -- see memory
DEFAULT_TIMEOUT = 10.0  # matches the Option A/B cutoff the benchmark used


def infer_trigger(event):
    s = event["signals"]
    if s["spn100_oil_pressure_kpa"] < OIL_KPA_MIN:
        return "low_oil_pressure"
    if s["spn110_coolant_temp_c"] > COOLANT_C_MAX:
        return "overheat"
    if s["spn190_engine_speed_rpm"] > RPM_MAX:
        return "overspeed"
    return "unknown"


FALLBACK_TEMPLATES = {
    "low_oil_pressure": (
        "Oil pressure of {spn100_oil_pressure_kpa:.0f} kPa is well below the "
        "~340 kPa baseline for {spn190_engine_speed_rpm:.0f} rpm, consistent "
        "with oil pump wear, low oil level, or a relief-valve fault. Recommend "
        "immediate oil level/pump inspection before continued operation."
    ),
    "overheat": (
        "Coolant temperature of {spn110_coolant_temp_c:.0f} C exceeds the safe "
        "operating limit, consistent with a cooling-system fault (low coolant, "
        "failing water pump, or blocked radiator). Recommend shutdown and "
        "cooling-system inspection before resuming operation."
    ),
    "overspeed": (
        "Engine speed of {spn190_engine_speed_rpm:.0f} rpm exceeds rated "
        "redline, consistent with a governor or throttle-control fault. "
        "Recommend immediate load reduction and governor/fuel-system inspection."
    ),
    "unknown": (
        "Signal readings on {asset_id} deviate from baseline (rpm="
        "{spn190_engine_speed_rpm:.0f}, coolant={spn110_coolant_temp_c:.0f}C, "
        "oil={spn100_oil_pressure_kpa:.0f}kPa, oil_temp={spn175_oil_temp_c:.0f}C). "
        "Recommend manual inspection to confirm root cause."
    ),
}


MONITOR_NOTE = (
    "Statistical anomaly detector flagged an unusual sensor pattern on "
    "{asset_id}, but no hard safety threshold was breached. Likely sensor "
    "noise or a borderline reading -- no confirmed fault, recommend routine "
    "monitoring rather than immediate action."
)


def fallback_summary(event):
    trigger = infer_trigger(event)
    fields = dict(event["signals"])
    fields["asset_id"] = event["asset_id"]
    return FALLBACK_TEMPLATES[trigger].format(**fields), trigger


def call_llm(host, model, prompt, timeout):
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": NUM_PREDICT, "temperature": 0.2},
    }
    url = host.rstrip("/") + "/api/generate"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def warm_up(host, model):
    print(f"Warming up {model} ...", flush=True)
    t0 = time.monotonic()
    try:
        call_llm(host, model, "Say OK.", timeout=120)
        print(f"  ready in {time.monotonic() - t0:.1f}s")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  warm-up failed: {e}", file=sys.stderr)
        return False


def summarize_event(host, model, event, timeout):
    if not event.get("confirmed", True):
        # ML-only flag, no hard threshold breached anywhere in the window --
        # don't let the LLM improvise a confident diagnosis for likely noise.
        text = MONITOR_NOTE.format(asset_id=event["asset_id"], **event["signals"])
        return {"source": "monitor", "model": None, "text": text, "latency_s": 0.0}

    prompt = format_anomaly_prompt(event)
    t0 = time.monotonic()
    try:
        resp = call_llm(host, model, prompt, timeout)
        elapsed = time.monotonic() - t0
        text = (resp.get("response") or "").strip()
        if not text:
            raise ValueError("empty response")
        return {"source": "llm", "model": model, "text": text, "latency_s": elapsed}
    except Exception as e:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        text, trigger = fallback_summary(event)
        print(f"    LLM call failed/too slow after {elapsed:.1f}s ({e}) "
              f"-- using '{trigger}' fallback template", file=sys.stderr)
        return {"source": "fallback", "model": None, "text": text, "latency_s": elapsed}


def detect_events(csv_path):
    rows = load_csv(csv_path)
    features = np.array([[float(r[c]) for c in FEATURE_COLUMNS] for r in rows])
    t_flags = threshold_flags(features)
    if_flags = isoforest_flags(features, contamination=0.08)
    combined = t_flags | if_flags
    return find_events(rows, features, combined, confirm_flags=t_flags)


def main():
    ap = argparse.ArgumentParser(
        description="Summarize detected J1939 anomaly events via a local LLM, with templated fallback."
    )
    ap.add_argument("--csv", default="data/demo_run.csv")
    ap.add_argument("--events-in", default=None,
                     help="Read pre-computed events from this JSON instead of re-running detection")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--out", default="data/summaries.json")
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()

    if args.events_in:
        with open(args.events_in) as f:
            events = json.load(f)
    else:
        events = detect_events(args.csv)

    if not events:
        print("No anomaly events to summarize.")
        return

    print(f"{len(events)} event(s) to summarize (model={args.model}, timeout={args.timeout}s)")
    if not args.no_warmup:
        warm_up(args.host, args.model)

    results = []
    for i, event in enumerate(events):
        print(f"\n[{i + 1}/{len(events)}] {event['start_ts']} asset={event['asset_id']}")
        result = summarize_event(args.host, args.model, event, args.timeout)
        result["event"] = event
        results.append(result)
        print(f"  ({result['source']}, {result['latency_s']:.2f}s): {result['text']}")

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)

    counts = {}
    for r in results:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    breakdown = ", ".join(f"{v} via {k}" for k, v in counts.items())
    print(f"\n{len(results)} event(s) summarized: {breakdown}.")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
