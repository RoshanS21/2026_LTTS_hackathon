#!/usr/bin/env python3
"""
anomaly_detector.py
====================
Piece 3 of the LTTS edge-AI demo: turn a J1939 signal CSV (piece 2's output,
or a live stream shaped the same way) into anomaly events.

Two detectors run in parallel, on purpose:
  - ThresholdDetector: fixed, hand-picked safety bounds. Always available,
    always deterministic, zero dependencies beyond stdlib. This is the
    demo-safe backstop -- it cannot fail to flag a hard-red-line condition
    just because a model didn't generalize.
  - IsolationForestDetector: unsupervised sklearn model (fixed random_state,
    so it's reproducible run-to-run) that also catches softer multi-signal
    drift the fixed thresholds don't cover.

A row is flagged if EITHER detector fires (maximizes recall -- for
predictive maintenance, a missed fault is worse than an extra alert). Each
contiguous flagged run becomes one "event", and format_anomaly_prompt()
turns an event into the same anomaly-summary prompt shape used in
benchmark_edge_llm.py's USER_PROMPT, so piece 4 (LLM summary layer) can
consume it directly.

USAGE
-----
  python3 anomaly_detector.py --csv data/demo_run.csv
  python3 anomaly_detector.py --csv data/demo_run.csv --stream --speed 30
"""

import argparse
import csv
import json
import sys
import time

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

FEATURE_COLUMNS = [
    "spn190_engine_speed_rpm",
    "spn110_coolant_temp_c",
    "spn100_oil_pressure_kpa",
    "spn175_oil_temp_c",
]

SPN_LABELS = {
    "spn190_engine_speed_rpm": ("SPN 190 Engine Speed", "rpm"),
    "spn110_coolant_temp_c": ("SPN 110 Engine Coolant Temp", "C"),
    "spn100_oil_pressure_kpa": ("SPN 100 Engine Oil Pressure", "kPa"),
    "spn175_oil_temp_c": ("SPN 175 Engine Oil Temp", "C"),
}

# Fixed safety bounds -- deliberately loose vs. the noisy baseline (see
# j1939_generator.py) so they don't false-positive on normal operating noise.
OIL_KPA_MIN = 150.0
COOLANT_C_MAX = 110.0
RPM_MAX = 2500.0

BASELINE_RPM = 2100.0
BASELINE_OIL_KPA = 340.0


def load_csv(path):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def threshold_flags(features):
    rpm, coolant, oil_kpa, _oil_temp = features.T
    return (oil_kpa < OIL_KPA_MIN) | (coolant > COOLANT_C_MAX) | (rpm > RPM_MAX)


def isoforest_flags(features, contamination, seed=42):
    scaler = StandardScaler()
    scaled = scaler.fit_transform(features)
    model = IsolationForest(
        n_estimators=200, contamination=contamination, random_state=seed
    )
    pred = model.fit_predict(scaled)  # -1 = anomaly, 1 = normal
    return pred == -1


def evaluate(name, flags, truth):
    tp = int(np.sum(flags & truth))
    fp = int(np.sum(flags & ~truth))
    fn = int(np.sum(~flags & truth))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    print(f"  {name:<18} precision={precision:.2f}  recall={recall:.2f}  f1={f1:.2f}")
    return precision, recall, f1


def find_events(rows, features, flags, confirm_flags=None, min_duration=3):
    """Contiguous flagged runs -> one event each, snapshotted at the row with
    the largest overall deviation from the dataset baseline (type-agnostic --
    a real detector doesn't know the injected label).

    `confirm_flags` (typically the deterministic threshold flags) marks an
    event "confirmed" if any row in its window also breached a hard safety
    bound. Events flagged only by the ML side (no threshold breach anywhere
    in the window) are "unconfirmed" -- soft/statistical alerts that
    downstream consumers should treat with less confidence.

    `min_duration` (rows) debounces the statistical detector: an isolated
    1-2s flagged blip is sensor noise, not a fault -- real faults persist.
    On this data every injected fault yields a >=20s run while every
    IsolationForest false positive is a 1-2s blip, so a 3s floor separates
    them perfectly. Runs shorter than the floor are dropped."""
    mean = features.mean(axis=0)
    std = features.std(axis=0) + 1e-9
    zscores = np.abs((features - mean) / std)
    severity = zscores.max(axis=1)

    events = []
    i = 0
    n = len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = i
        while i < n and flags[i]:
            i += 1
        end = i  # exclusive
        if end - start < min_duration:
            continue
        peak = start + int(np.argmax(severity[start:end]))
        confirmed = bool(confirm_flags[start:end].any()) if confirm_flags is not None else True
        events.append({
            "start_ts": rows[start]["timestamp"],
            "end_ts": rows[end - 1]["timestamp"],
            "asset_id": rows[peak]["asset_id"],
            "duration_s": end - start,
            "start_row": start,
            "end_row": end - 1,
            "peak_row": peak,
            "confirmed": confirmed,
            "signals": {
                col: float(rows[peak][col]) for col in FEATURE_COLUMNS
            },
        })
    return events


def truth_windows(truth):
    """Contiguous ground-truth anomaly runs -> list of (start, end) row spans
    (end exclusive). These are the injected fault windows."""
    windows = []
    i = 0
    n = len(truth)
    while i < n:
        if not truth[i]:
            i += 1
            continue
        start = i
        while i < n and truth[i]:
            i += 1
        windows.append((start, i))
    return windows


def event_metrics(events, truth):
    """Event-level scoring -- the number that matters for maintenance: of the
    injected fault windows, how many produced an event (caught), and how many
    events matched no real fault (false alarms)."""
    windows = truth_windows(truth)
    caught = sum(
        1 for (ws, we) in windows
        if any(e["start_row"] < we and e["end_row"] >= ws for e in events)
    )
    false_alarms = sum(
        1 for e in events
        if not any(e["start_row"] < we and e["end_row"] >= ws for (ws, we) in windows)
    )
    return caught, len(windows), false_alarms


def format_anomaly_prompt(event):
    """Match benchmark_edge_llm.py's USER_PROMPT shape so piece 4 can feed
    this straight into the LLM summary call."""
    lines = [
        f"Anomaly flagged on asset {event['asset_id']}.",
        "Signal frame (J1939):",
    ]
    s = event["signals"]
    flagged = []
    for col in FEATURE_COLUMNS:
        label, unit = SPN_LABELS[col]
        value = s[col]
        flag = ""
        if col == "spn100_oil_pressure_kpa" and value < OIL_KPA_MIN:
            flag = "  <-- LOW for this rpm"
        elif col == "spn110_coolant_temp_c" and value > COOLANT_C_MAX:
            flag = "  <-- HIGH"
        elif col == "spn190_engine_speed_rpm" and value > RPM_MAX:
            flag = "  <-- OVER REDLINE"
        if flag:
            flagged.append(label)
        lines.append(f"  {label}: {value:.0f} {unit}{flag}")
    # Small on-device models latch onto whatever context is offered, so only
    # mention the oil baseline when oil pressure is actually the flagged
    # signal, and name the flagged signal(s) explicitly in the instruction.
    if "SPN 100 Engine Oil Pressure" in flagged:
        lines.append(
            f"Baseline oil pressure at {BASELINE_RPM:.0f} rpm is "
            f"~{BASELINE_OIL_KPA:.0f} kPa."
        )
    target = " and ".join(flagged) if flagged else "the deviating signals"
    lines.append(f"Summarize the likely fault behind {target} and the "
                 "recommended action.")
    return "\n".join(lines)


def run_stream(rows, features, combined_flags, speed):
    print(f"\n--- streaming {len(rows)} rows (speed={speed}x) ---")
    last_ts = None
    active_event_start = None
    for i, row in enumerate(rows):
        if speed > 0 and last_ts is not None:
            from datetime import datetime
            cur = datetime.fromisoformat(row["timestamp"])
            prev = datetime.fromisoformat(last_ts)
            dt = (cur - prev).total_seconds()
            time.sleep(max(0.0, dt / speed))
        last_ts = row["timestamp"]

        if combined_flags[i] and active_event_start is None:
            active_event_start = i
            print(f"  row {i} [{row['timestamp']}]  ANOMALY START")
        elif not combined_flags[i] and active_event_start is not None:
            print(f"  row {i} [{row['timestamp']}]  anomaly cleared "
                  f"({i - active_event_start}s)")
            active_event_start = None
    if active_event_start is not None:
        print(f"  (stream ended mid-anomaly, started row {active_event_start})")


def main():
    ap = argparse.ArgumentParser(
        description="Detect anomalies in a J1939 signal CSV (threshold + IsolationForest)."
    )
    ap.add_argument("--csv", default="data/demo_run.csv")
    ap.add_argument("--events-out", default="data/detected_events.json")
    ap.add_argument("--contamination", type=float, default=0.08)
    ap.add_argument("--min-duration", type=int, default=3,
                     help="Debounce: drop flagged runs shorter than this many rows")
    ap.add_argument("--stream", action="store_true",
                     help="Print a simulated live pass over the rows instead of a batch report")
    ap.add_argument("--speed", type=float, default=50.0,
                     help="Stream playback speed multiplier (higher = faster). 0 = no delay")
    args = ap.parse_args()

    rows = load_csv(args.csv)
    if not rows:
        print(f"ERROR: no rows in {args.csv}", file=sys.stderr)
        sys.exit(1)

    features = np.array([[float(r[c]) for c in FEATURE_COLUMNS] for r in rows])
    truth = np.array([r["is_anomaly"] == "1" for r in rows])

    t_flags = threshold_flags(features)
    if_flags = isoforest_flags(features, args.contamination)
    combined = t_flags | if_flags

    if args.stream:
        run_stream(rows, features, combined, args.speed)
        return

    print(f"Loaded {len(rows)} rows from {args.csv} "
          f"({int(truth.sum())} ground-truth anomalous rows)")
    print("\nDetector performance vs. ground truth:")
    evaluate("threshold-only", t_flags, truth)
    evaluate("isoforest-only", if_flags, truth)
    evaluate("combined (OR)", combined, truth)

    raw_events = find_events(rows, features, combined, confirm_flags=t_flags,
                             min_duration=1)
    events = find_events(rows, features, combined, confirm_flags=t_flags,
                         min_duration=args.min_duration)
    debounced = len(raw_events) - len(events)
    caught, total, false_alarms = event_metrics(events, truth)
    print(f"\nEvent-level (what a maintenance team acts on):")
    print(f"  fault windows caught: {caught}/{total}   "
          f"false-alarm events: {false_alarms}   "
          f"noise blips debounced (<{args.min_duration}s): {debounced}")

    print(f"\n{len(events)} anomaly event(s) detected:")
    for e in events:
        tag = "confirmed" if e["confirmed"] else "unconfirmed (ML-only)"
        print(f"  [{e['start_ts']} -> {e['end_ts']}]  {e['duration_s']}s  "
              f"asset={e['asset_id']}  {tag}")

    with open(args.events_out, "w") as f:
        json.dump(events, f, indent=2)
    print(f"\nWrote {len(events)} event(s) to {args.events_out}")

    if events:
        print("\nSample LLM prompt for the first detected event:")
        print("  " + format_anomaly_prompt(events[0]).replace("\n", "\n  "))


if __name__ == "__main__":
    main()
