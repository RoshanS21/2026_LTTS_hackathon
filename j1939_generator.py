#!/usr/bin/env python3
"""
j1939_generator.py
===================
Generate a synthetic J1939 signal-stream CSV for the fleet asset used in the
LTTS edge-AI demo, with reproducible injected anomalies (fixed --seed) so the
anomaly detector (piece 3) and LLM summary layer (piece 4) can be built and
validated against known ground truth before real hardware/data is wired in.

Signals mirror the SPNs already used in benchmark_edge_llm.py's demo prompt:
  SPN 190 Engine Speed, SPN 110 Engine Coolant Temp,
  SPN 100 Engine Oil Pressure, SPN 175 Engine Oil Temp.

USAGE
-----
  python3 j1939_generator.py
  python3 j1939_generator.py --out data/demo_run.csv --duration-min 30 --hz 1 --seed 13 --anomalies 5
"""

import argparse
import csv
import os
import random
from datetime import datetime, timedelta, timezone

import numpy as np

DEFAULT_ASSET_ID = "DEERE-7R-014"

# Baseline operating point -- matches the "2100 rpm / ~340 kPa" anomaly prompt
# in benchmark_edge_llm.py so downstream pieces describe the same normal state.
BASE_RPM = 2100.0
BASE_COOLANT_C = 92.0
BASE_OIL_KPA = 340.0
BASE_OIL_TEMP_C = 98.0

ANOMALY_TYPES = ("low_oil_pressure", "overheat", "overspeed")
ANOMALY_DURATION_S = (20, 60)  # inclusive range, in seconds of wall time


def simulate_baseline(n, hz, rng):
    t = np.arange(n) / hz

    rpm = BASE_RPM + 40 * np.sin(t / 180.0) + rng.normal(0, 15, n)
    coolant = BASE_COOLANT_C + 3 * np.sin(t / 300.0 + 1.0) + rng.normal(0, 0.8, n)
    oil_temp = BASE_OIL_TEMP_C + 3 * np.sin(t / 300.0 + 1.2) + rng.normal(0, 0.8, n)
    # Oil pressure normally tracks rpm roughly linearly around the baseline point.
    oil_kpa = BASE_OIL_KPA + (rpm - BASE_RPM) * 0.9 + rng.normal(0, 6, n)

    return rpm, coolant, oil_kpa, oil_temp


def inject_anomalies(rpm, coolant, oil_kpa, oil_temp, hz, count, rng, py_rng):
    n = len(rpm)
    labels = np.zeros(n, dtype=int)
    types = [""] * n

    min_dur = int(ANOMALY_DURATION_S[0] * hz)
    max_dur = int(ANOMALY_DURATION_S[1] * hz)
    windows = []

    attempts = 0
    while len(windows) < count and attempts < count * 50:
        attempts += 1
        dur = py_rng.randint(min_dur, max_dur)
        start = py_rng.randint(0, n - dur - 1)
        end = start + dur
        if any(not (end < w[0] or start > w[1]) for w in windows):
            continue
        windows.append((start, end))
    windows.sort()

    for start, end in windows:
        kind = py_rng.choice(ANOMALY_TYPES)
        idx = slice(start, end)
        labels[idx] = 1
        for i in range(start, end):
            types[i] = kind
        span = end - start

        if kind == "low_oil_pressure":
            oil_kpa[idx] = rng.uniform(25, 55, span)
            oil_temp[idx] += np.linspace(0, 25, span)
        elif kind == "overheat":
            coolant[idx] += np.linspace(0, 28, span)
            oil_temp[idx] += np.linspace(0, 15, span)
        elif kind == "overspeed":
            rpm[idx] += np.linspace(0, 550, span)
            oil_kpa[idx] += np.linspace(0, 90, span)

    return labels, types, windows


def main():
    ap = argparse.ArgumentParser(
        description="Generate a synthetic J1939 CSV with injected anomalies."
    )
    ap.add_argument("--out", default="data/demo_run.csv")
    ap.add_argument("--asset-id", default=DEFAULT_ASSET_ID)
    ap.add_argument("--duration-min", type=float, default=30.0)
    ap.add_argument("--hz", type=float, default=1.0, help="Samples per second")
    ap.add_argument("--anomalies", type=int, default=5)
    # Default seed picked deliberately: 13 injects all three anomaly types
    # (2x low_oil_pressure, 2x overheat, 1x overspeed) spread across the run,
    # so the demo shows fault-type variety. (Seed 42 gives 5x low_oil_pressure.)
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    py_rng = random.Random(args.seed)

    n = int(args.duration_min * 60 * args.hz)
    rpm, coolant, oil_kpa, oil_temp = simulate_baseline(n, args.hz, rng)
    labels, types, windows = inject_anomalies(
        rpm, coolant, oil_kpa, oil_temp, args.hz, args.anomalies, rng, py_rng
    )

    start_time = datetime.now(timezone.utc).replace(microsecond=0)
    step = timedelta(seconds=1.0 / args.hz)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "asset_id",
            "spn190_engine_speed_rpm", "spn110_coolant_temp_c",
            "spn100_oil_pressure_kpa", "spn175_oil_temp_c",
            "is_anomaly", "anomaly_type",
        ])
        for i in range(n):
            ts = (start_time + i * step).isoformat()
            writer.writerow([
                ts, args.asset_id,
                round(float(rpm[i]), 1), round(float(coolant[i]), 1),
                round(float(oil_kpa[i]), 1), round(float(oil_temp[i]), 1),
                labels[i], types[i],
            ])

    print(f"Wrote {n} rows ({args.duration_min:.0f} min @ {args.hz}Hz) to {args.out}")
    print(f"Injected {len(windows)} anomaly windows (seed={args.seed}):")
    for start, end in windows:
        dur_s = (end - start) / args.hz
        print(f"  rows {start}-{end}  ({dur_s:.0f}s)  type={types[start]}")


if __name__ == "__main__":
    main()
