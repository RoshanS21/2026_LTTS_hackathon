#!/usr/bin/env python3
"""
j1939_generator.py
===================
Generate synthetic J1939 signal-stream CSVs for the LTTS edge-AI demo, with
reproducible injected anomalies (fixed --seed) so the anomaly detector
(piece 3) and LLM summary layer (piece 4) can be built and validated against
known ground truth before real hardware/data is wired in.

Signals mirror the SPNs already used in benchmark_edge_llm.py's demo prompt:
  SPN 190 Engine Speed, SPN 110 Engine Coolant Temp,
  SPN 100 Engine Oil Pressure, SPN 175 Engine Oil Temp.

SCENARIOS -- each one is a different judge-facing story:
  mixed        The default demo: 5 hard faults across all three types.
  healthy      No faults at all, only 1-2s sensor glitches (labeled NOT
               anomalous). Story: the system stays quiet on a healthy
               machine -- glitches are debounced, zero alarms fire.
  degradation  One slow 8-minute oil-pressure decline (pump wear). Story:
               predictive maintenance -- the statistical detector flags the
               drift MINUTES before the hard safety threshold breaks, the
               dashboard escalates MONITOR -> ALARM live and reports the
               lead time.
  stress       7 hard faults of all types packed into 30 minutes plus
               sensor glitches. Story: throughput -- every fault caught,
               glitches still debounced, the edge LLM keeps up.

Sensor glitches deliberately stay INSIDE the hard safety bounds (a glitch
must never trip the no-debounce threshold path), and are written with
is_anomaly=0: flagging them would be a detector false positive.

USAGE
-----
  python3 j1939_generator.py                          # mixed -> data/demo_run.csv
  python3 j1939_generator.py --scenario degradation   # -> data/scenarios/degradation.csv
  python3 j1939_generator.py --all                    # mixed + every scenario CSV
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

FAULT_KINDS = ("low_oil_pressure", "overheat", "overspeed")
# Glitches: single-sensor misreads, 1-2s, all within hard safety bounds.
GLITCH_KINDS = ("coolant_low_read", "rpm_jitter", "oil_temp_spike")

FAULT_DURATION_S = (20, 60)   # hard faults, inclusive range in wall seconds
GLITCH_DURATION_S = (1, 2)
DECLINE_DURATION_S = 480      # the slow-degradation window (8 min)


def simulate_baseline(n, hz, rng):
    t = np.arange(n) / hz

    rpm = BASE_RPM + 40 * np.sin(t / 180.0) + rng.normal(0, 15, n)
    coolant = BASE_COOLANT_C + 3 * np.sin(t / 300.0 + 1.0) + rng.normal(0, 0.8, n)
    oil_temp = BASE_OIL_TEMP_C + 3 * np.sin(t / 300.0 + 1.2) + rng.normal(0, 0.8, n)
    # Oil pressure normally tracks rpm roughly linearly around the baseline point.
    oil_kpa = BASE_OIL_KPA + (rpm - BASE_RPM) * 0.9 + rng.normal(0, 6, n)

    return rpm, coolant, oil_kpa, oil_temp


def apply_window(kind, sl, rpm, coolant, oil_kpa, oil_temp, rng):
    span = sl.stop - sl.start
    if kind == "low_oil_pressure":
        oil_kpa[sl] = rng.uniform(25, 55, span)
        oil_temp[sl] += np.linspace(0, 25, span)
    elif kind == "overheat":
        coolant[sl] += np.linspace(0, 28, span)
        oil_temp[sl] += np.linspace(0, 15, span)
    elif kind == "overspeed":
        rpm[sl] += np.linspace(0, 550, span)
        oil_kpa[sl] += np.linspace(0, 90, span)
    elif kind == "oil_decline":
        # Slow pump-wear drift: baseline ~340 kPa down to ~120 kPa. Only the
        # final ~15% of the window sits below the 150 kPa hard bound, so the
        # statistical detector has minutes of visible drift to flag before
        # the deterministic threshold ever fires -- the predictive story.
        oil_kpa[sl] -= np.linspace(0, 220, span)
        oil_temp[sl] += np.linspace(0, 22, span)
    elif kind == "coolant_low_read":
        coolant[sl] -= 45.0        # sensor dropout-style low read; no min bound
    elif kind == "rpm_jitter":
        rpm[sl] += 280.0           # spike, but stays under the 2500 redline
    elif kind == "oil_temp_spike":
        oil_temp[sl] += 30.0       # oil temp has no hard bound at all
    else:
        raise ValueError(f"unknown anomaly kind: {kind}")


def place_windows(py_rng, n, specs, occupied):
    """Place non-overlapping windows. `specs` is a list of
    (kind, min_dur, max_dur, is_fault); returns [(kind, start, end, is_fault)].
    `occupied` is a shared list of (start, end) spans, mutated in place."""
    out = []
    for kind, min_dur, max_dur, is_fault in specs:
        for _ in range(500):
            dur = py_rng.randint(min_dur, max_dur)
            start = py_rng.randint(0, n - dur - 1)
            end = start + dur
            if any(not (end < s or start > e) for s, e in occupied):
                continue
            occupied.append((start, end))
            out.append((kind, start, end, is_fault))
            break
    return out


def build_plan(scenario, n, hz, py_rng, fault_count):
    fmin = int(FAULT_DURATION_S[0] * hz)
    fmax = int(FAULT_DURATION_S[1] * hz)
    gmin = int(GLITCH_DURATION_S[0] * hz)
    gmax = int(GLITCH_DURATION_S[1] * hz)
    occupied = []

    if scenario == "mixed":
        specs = [(py_rng.choice(FAULT_KINDS), fmin, fmax, True)
                 for _ in range(fault_count)]
    elif scenario == "healthy":
        specs = [(py_rng.choice(GLITCH_KINDS), gmin, gmax, False)
                 for _ in range(4)]
    elif scenario == "degradation":
        # One long decline placed mid-run so there's healthy baseline both
        # sides, plus a couple of glitches the debounce should eat.
        dur = int(DECLINE_DURATION_S * hz)
        start = int(n * 0.35)
        occupied.append((start, start + dur))
        specs = [(py_rng.choice(GLITCH_KINDS), gmin, gmax, False)
                 for _ in range(2)]
        return ([("oil_decline", start, start + dur, True)]
                + place_windows(py_rng, n, specs, occupied))
    elif scenario == "stress":
        specs = ([(py_rng.choice(FAULT_KINDS), fmin, fmax, True)
                  for _ in range(7)]
                 + [(py_rng.choice(GLITCH_KINDS), gmin, gmax, False)
                    for _ in range(5)])
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    return place_windows(py_rng, n, specs, occupied)


def generate(scenario, out, asset_id, duration_min, hz, seed, fault_count):
    rng = np.random.default_rng(seed)
    py_rng = random.Random(seed)

    n = int(duration_min * 60 * hz)
    rpm, coolant, oil_kpa, oil_temp = simulate_baseline(n, hz, rng)

    plan = sorted(build_plan(scenario, n, hz, py_rng, fault_count),
                  key=lambda w: w[1])
    labels = np.zeros(n, dtype=int)
    types = [""] * n
    for kind, start, end, is_fault in plan:
        apply_window(kind, slice(start, end), rpm, coolant, oil_kpa, oil_temp, rng)
        for i in range(start, end):
            types[i] = kind
        if is_fault:
            labels[start:end] = 1

    start_time = datetime.now(timezone.utc).replace(microsecond=0)
    step = timedelta(seconds=1.0 / hz)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="") as f:
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
                ts, asset_id,
                round(float(rpm[i]), 1), round(float(coolant[i]), 1),
                round(float(oil_kpa[i]), 1), round(float(oil_temp[i]), 1),
                labels[i], types[i],
            ])

    print(f"[{scenario}] wrote {n} rows ({duration_min:.0f} min @ {hz}Hz) to {out}")
    for kind, start, end, is_fault in plan:
        dur_s = (end - start) / hz
        tag = "fault" if is_fault else "glitch (not a fault)"
        print(f"  rows {start}-{end}  ({dur_s:.0f}s)  {kind}  [{tag}]")


SCENARIOS = ("mixed", "healthy", "degradation", "stress")


def main():
    ap = argparse.ArgumentParser(
        description="Generate synthetic J1939 CSVs with injected anomalies, by scenario."
    )
    ap.add_argument("--scenario", choices=SCENARIOS, default="mixed")
    ap.add_argument("--all", action="store_true",
                    help="Generate every scenario (mixed -> data/demo_run.csv, "
                         "others -> data/scenarios/<name>.csv)")
    ap.add_argument("--out", default=None,
                    help="Output CSV (default: data/demo_run.csv for mixed, "
                         "data/scenarios/<scenario>.csv otherwise)")
    ap.add_argument("--asset-id", default=DEFAULT_ASSET_ID)
    ap.add_argument("--duration-min", type=float, default=30.0)
    ap.add_argument("--hz", type=float, default=1.0, help="Samples per second")
    ap.add_argument("--anomalies", type=int, default=5,
                    help="Fault count (mixed scenario only)")
    # Default seed picked deliberately: 13 injects all three fault types in
    # the mixed scenario (2x low_oil_pressure, 2x overheat, 1x overspeed)
    # spread across the run. (Seed 42 gives 5x low_oil_pressure.)
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    def default_out(scenario):
        return ("data/demo_run.csv" if scenario == "mixed"
                else f"data/scenarios/{scenario}.csv")

    if args.all:
        for scenario in SCENARIOS:
            generate(scenario, default_out(scenario), args.asset_id,
                     args.duration_min, args.hz, args.seed, args.anomalies)
    else:
        out = args.out or default_out(args.scenario)
        generate(args.scenario, out, args.asset_id,
                 args.duration_min, args.hz, args.seed, args.anomalies)


if __name__ == "__main__":
    main()
