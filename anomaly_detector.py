#!/usr/bin/env python3
"""
anomaly_detector.py
====================
Piece 3 of the LTTS edge-AI demo: turn a J1939 signal CSV (piece 2's output,
or a live stream shaped the same way) into anomaly events.

Three detectors run in parallel, on purpose -- each covers a failure mode
the others miss:
  - ThresholdDetector: fixed, hand-picked safety bounds. Always available,
    always deterministic, zero dependencies beyond stdlib. This is the
    demo-safe backstop -- it cannot fail to flag a hard-red-line condition
    just because a model didn't generalize.
  - IsolationForestDetector: unsupervised sklearn model (fixed random_state,
    so it's reproducible run-to-run) that catches multivariate outliers the
    fixed thresholds don't cover.
  - CUSUMDetector: classic industrial change detection. Accumulates small
    persistent deviations from the baseline, so it catches SLOW DRIFTS
    (pump wear, gradual coolant loss) minutes before any hard bound breaks
    -- the genuinely predictive tier. Sensor noise never sustains the
    accumulation, so it stays quiet on a healthy machine.

A row is flagged if ANY detector fires (maximizes recall -- for predictive
maintenance, a missed fault is worse than an extra alert). Flagged runs are
merged across short clear gaps, debounced, and become "events";
format_anomaly_prompt() turns an event into the same anomaly-summary prompt
shape used in benchmark_edge_llm.py's USER_PROMPT, so piece 4 (LLM summary
layer) can consume it directly.

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


# CUSUM tuning, validated by a scenario sweep (healthy stays at zero events,
# the slow-decline scenario is flagged ~190s before its hard threshold
# breach): k must sit above the baseline's slow sinusoidal wander (~1.3
# robust-z on coolant), h sets how much sustained deviation accumulates
# before firing, and the cap bounds how long the score lingers after a
# fault clears.
CUSUM_K = 2.0
CUSUM_H = 8.0
CUSUM_CAP_MULT = 1.5


def cusum_baseline(features):
    """Robust per-signal location/scale (median / scaled MAD) -- the
    'pre-trained' baseline the streaming CUSUM measures deviations against.
    Robust stats keep a fault window in the training data from dragging the
    baseline toward itself."""
    med = np.median(features, axis=0)
    mad = np.median(np.abs(features - med), axis=0) * 1.4826 + 1e-9
    return med, mad


class CusumDetector:
    """Two-sided CUSUM over robust z-scores, one accumulator pair per signal.
    step() is O(signals) per row -- built for streaming."""

    def __init__(self, med, mad, k=CUSUM_K, h=CUSUM_H, cap_mult=CUSUM_CAP_MULT):
        self.med, self.mad, self.k, self.h = med, mad, k, h
        self.cap = h * cap_mult
        self.s_pos = np.zeros(len(med))
        self.s_neg = np.zeros(len(med))

    def step(self, values):
        z = (values - self.med) / self.mad
        self.s_pos = np.minimum(np.maximum(0.0, self.s_pos + z - self.k), self.cap)
        self.s_neg = np.minimum(np.maximum(0.0, self.s_neg - z - self.k), self.cap)
        return bool((self.s_pos > self.h).any() or (self.s_neg > self.h).any())


def cusum_flags(features, k=CUSUM_K, h=CUSUM_H, cap_mult=CUSUM_CAP_MULT):
    """Batch equivalent of streaming CusumDetector.step() over every row."""
    det = CusumDetector(*cusum_baseline(features), k=k, h=h, cap_mult=cap_mult)
    return np.array([det.step(row) for row in features])


def evaluate(name, flags, truth):
    tp = int(np.sum(flags & truth))
    fp = int(np.sum(flags & ~truth))
    fn = int(np.sum(~flags & truth))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    print(f"  {name:<18} precision={precision:.2f}  recall={recall:.2f}  f1={f1:.2f}")
    return precision, recall, f1


def find_events(rows, features, flags, confirm_flags=None, min_duration=3,
                min_unconfirmed=8, merge_gap=3):
    """Flagged runs -> one event each, snapshotted at the row with the
    largest overall deviation from the dataset baseline (type-agnostic --
    a real detector doesn't know the injected label).

    `confirm_flags` (typically the deterministic threshold flags) marks an
    event "confirmed" if any row in its window also breached a hard safety
    bound; `confirm_lead_s` records how long the statistical side had been
    flagging before that first hard breach -- the predictive lead time.
    Events flagged only by the statistical side (no threshold breach
    anywhere in the window) are "unconfirmed" -- soft alerts that downstream
    consumers should treat with less confidence.

    Hysteresis keeps events clean, with all floors counted in FLAGGED rows
    (a merged pair of 1s blips is still just 2 rows of evidence):
      - `merge_gap` (rows): runs separated by a short clear gap are the same
        physical fault flickering around the detection boundary, so they
        merge into one event instead of spamming one card per flicker.
      - `min_duration`: evidence floor for confirmed events. A hard safety
        breach is strong evidence, so this stays low.
      - `min_unconfirmed`: evidence floor for statistical-only events. A
        pure-ML claim needs sustained support; on this data real drifts
        sustain 100+ flagged rows while noise chains muster a handful, so
        an 8-row floor separates them (validated across all scenarios)."""
    mean = features.mean(axis=0)
    std = features.std(axis=0) + 1e-9
    zscores = np.abs((features - mean) / std)
    severity = zscores.max(axis=1)

    # Contiguous flagged runs...
    runs = []
    i = 0
    n = len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = i
        while i < n and flags[i]:
            i += 1
        runs.append([start, i, i - start])  # end exclusive, flagged count
    # ...merged across short clear gaps...
    merged = []
    for start, end, count in runs:
        if merged and start - merged[-1][1] <= merge_gap:
            merged[-1][1] = end
            merged[-1][2] += count
        else:
            merged.append([start, end, count])

    # ...then debounced (per-tier evidence floors) and turned into events.
    events = []
    for start, end, count in merged:
        confirmed, lead = True, 0
        if confirm_flags is not None:
            window = confirm_flags[start:end]
            confirmed = bool(window.any())
            lead = int(np.argmax(window)) if confirmed else None
        if count < (min_duration if confirmed else min_unconfirmed):
            continue
        peak = start + int(np.argmax(severity[start:end]))
        events.append({
            "start_ts": rows[start]["timestamp"],
            "end_ts": rows[end - 1]["timestamp"],
            "asset_id": rows[peak]["asset_id"],
            "duration_s": end - start,
            "start_row": start,
            "end_row": end - 1,
            "peak_row": peak,
            "confirmed": confirmed,
            "confirm_lead_s": lead,
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
    """Event-level scoring -- the numbers that matter for maintenance: of the
    injected fault windows, how many produced an event (caught); how many
    CONFIRMED events matched no real fault (false alarms -- these fire the
    GPIO alarm and cost an LLM call); and how many unconfirmed events matched
    no real fault (soft flags -- monitor notes only, cheap by design)."""
    windows = truth_windows(truth)

    def overlaps_truth(e):
        return any(e["start_row"] < we and e["end_row"] >= ws
                   for (ws, we) in windows)

    caught = sum(
        1 for (ws, we) in windows
        if any(e["start_row"] < we and e["end_row"] >= ws for e in events)
    )
    false_alarms = sum(1 for e in events
                       if e["confirmed"] and not overlaps_truth(e))
    soft_flags = sum(1 for e in events
                     if not e["confirmed"] and not overlaps_truth(e))
    return caught, len(windows), false_alarms, soft_flags


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
                     help="Evidence floor (flagged rows) for confirmed events")
    ap.add_argument("--min-unconfirmed", type=int, default=8,
                     help="Evidence floor (flagged rows) for statistical-only events")
    ap.add_argument("--merge-gap", type=int, default=3,
                     help="Hysteresis: merge flagged runs separated by clear "
                          "gaps up to this many rows")
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
    cu_flags = cusum_flags(features)
    combined = t_flags | if_flags | cu_flags

    if args.stream:
        run_stream(rows, features, combined, args.speed)
        return

    print(f"Loaded {len(rows)} rows from {args.csv} "
          f"({int(truth.sum())} ground-truth anomalous rows)")
    print("\nDetector performance vs. ground truth:")
    evaluate("threshold-only", t_flags, truth)
    evaluate("isoforest-only", if_flags, truth)
    evaluate("cusum-only", cu_flags, truth)
    evaluate("combined (OR)", combined, truth)

    raw_events = find_events(rows, features, combined, confirm_flags=t_flags,
                             min_duration=1, min_unconfirmed=1,
                             merge_gap=args.merge_gap)
    events = find_events(rows, features, combined, confirm_flags=t_flags,
                         min_duration=args.min_duration,
                         min_unconfirmed=args.min_unconfirmed,
                         merge_gap=args.merge_gap)
    debounced = len(raw_events) - len(events)
    caught, total, false_alarms, soft_flags = event_metrics(events, truth)
    print(f"\nEvent-level (what a maintenance team acts on):")
    print(f"  fault windows caught: {caught}/{total}   "
          f"false alarms (confirmed, no real fault): {false_alarms}   "
          f"soft flags (monitor-only, no real fault): {soft_flags}   "
          f"noise blips debounced: {debounced}")

    print(f"\n{len(events)} anomaly event(s) detected:")
    for e in events:
        tag = "confirmed" if e["confirmed"] else "unconfirmed (ML-only)"
        lead = ""
        if e["confirmed"] and e["confirm_lead_s"]:
            lead = f"  (ML flagged {e['confirm_lead_s']}s before hard breach)"
        print(f"  [{e['start_ts']} -> {e['end_ts']}]  {e['duration_s']}s  "
              f"asset={e['asset_id']}  {tag}{lead}")

    with open(args.events_out, "w") as f:
        json.dump(events, f, indent=2)
    print(f"\nWrote {len(events)} event(s) to {args.events_out}")

    if events:
        print("\nSample LLM prompt for the first detected event:")
        print("  " + format_anomaly_prompt(events[0]).replace("\n", "\n  "))


if __name__ == "__main__":
    main()
