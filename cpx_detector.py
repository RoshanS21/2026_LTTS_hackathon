#!/usr/bin/env python3
"""
cpx_detector.py
===============
Piece 3 for the LIVE hardware path: the CPX signal profile for the anomaly
detector. Same three-tier detection core as anomaly_detector.py (threshold +
IsolationForest + CUSUM, event forming, hysteresis, lead time) -- this module
only supplies what's specific to the Circuit Playground Express sensor edge:
the signal columns, the hard-safety bounds, the fault semantics, and the
LLM-ready prompt. Everything else is imported and reused, so the J1939
simulated pipeline and this hardware pipeline can't drift apart.

Signals (from cpx_serial_reader.py frames):
  accel_mag_ms2  vibration -- magnitude of the accel vector. Rests at ~9.8
                 (gravity) regardless of board orientation; DEVIATION from
                 gravity (a shake, in either direction) is the fault signal.
  temp_c         overheat -- board thermistor. Warms slowly (self-heat +
                 ambient), so a hairdryer breaches the hard bound while gentle
                 hand-warmth shows up on the drift (CUSUM) tier instead.
  sound_level    loudness -- mean-subtracted mic RMS. Spikes on a loud event.

button_a is NOT a sensor feature. It's the honest manual-confirm trigger
(see firmware/cpx_sensor.py): a held button both OPENS an event and CONFIRMS
it, exactly as a hard safety breach does -- the demo-safety fallback if a
physical shake/warm doesn't land on stage. Events carry `manual_trigger` so
the summary layer describes a manual inspection request instead of inventing
a sensor fault.

Real hardware has no ground-truth labels, so unlike anomaly_detector.py this
reports detected events without precision/recall.

USAGE
-----
  python3 cpx_detector.py --csv data/cpx_live_run.csv
  python3 cpx_detector.py --csv data/cpx_live_run.csv --events-out data/cpx_events.json
"""

import argparse
import json
import sys
from datetime import datetime

import numpy as np

from anomaly_detector import (
    CusumDetector, cusum_baseline, cusum_flags, find_events, isoforest_flags,
    load_csv,
)

FEATURE_COLUMNS = [
    "accel_mag_ms2",
    "temp_c",
    "sound_level",
]

CPX_LABELS = {
    "accel_mag_ms2": ("Vibration (accel magnitude)", "m/s^2"),
    "temp_c": ("Board Temperature", "C"),
    "sound_level": ("Acoustic Level (mic RMS)", ""),
}

# Hard safety bounds, grounded in a real 447-frame capture (see the profiling
# in this piece's dev notes): at rest |accel-g| stays under ~4 while a shake
# exceeds 30; ambient/hand-warmth tops out ~30.5 C so a hairdryer is needed to
# breach TEMP; resting mic RMS stays under ~200 while a clap/shout hits >1000.
GRAVITY = 9.8
VIBRATION_DEV_MAX = 8.0   # |accel_mag - GRAVITY| above this = abnormal motion
TEMP_C_MAX = 32.0         # just above the ~30.5 C hand-warmth ceiling (see
                          # the profiling note above) -- sustained firm
                          # cupping may occasionally breach it, but a
                          # hairdryer a few inches away is the reliable
                          # trigger for a confirmed overheat alarm on stage
SOUND_MAX = 1000.0        # loud acoustic event

BASELINE_TEMP_C = 27.0    # typical warmed-up idle, for the prompt's context

SAMPLE_HZ = 10.0          # CPX firmware sample rate -- the shared find_events
                          # counts evidence in ROWS; at 10 Hz that is not
                          # seconds, so durations/lead times are converted below.


def _duration_s(event):
    """True elapsed seconds from the Pi wall-clock timestamps -- robust to the
    10 Hz sample rate and to any dropped frames, unlike a raw row count."""
    t0 = datetime.fromisoformat(event["start_ts"])
    t1 = datetime.fromisoformat(event["end_ts"])
    return round((t1 - t0).total_seconds(), 1)


def threshold_flags_cpx(features):
    """Deterministic hard-safety net for the CPX signals. Vibration is a
    two-sided deviation from gravity; temp and sound are one-sided highs."""
    accel_mag, temp, sound = features.T
    return (
        (np.abs(accel_mag - GRAVITY) > VIBRATION_DEV_MAX)
        | (temp > TEMP_C_MAX)
        | (sound > SOUND_MAX)
    )


def detect(rows, contamination=0.08, min_duration=3, min_unconfirmed=8,
           merge_gap=3):
    """Run the three-tier detector over CPX frames plus the manual button
    trigger. Returns (events, flag_breakdown).

    button_a is folded in as both a flag source (it opens an event) and a
    confirm source (it confirms one), so a held button produces a confirmed
    event even with quiet sensors -- its whole point as a demo-safety trigger.
    Each event gets `manual_trigger` = was the button held anywhere in its
    window."""
    features = np.array([[float(r[c]) for c in FEATURE_COLUMNS] for r in rows])
    button = np.array([int(r.get("button_a", 0)) == 1 for r in rows])

    t_flags = threshold_flags_cpx(features)
    if_flags = isoforest_flags(features, contamination)
    cu_flags = cusum_flags(features)

    # Sensor-detected flags vs. the manual trigger, kept distinct so we can
    # tell a real sensor anomaly from an operator pressing the button.
    sensor_flags = t_flags | if_flags | cu_flags
    combined = sensor_flags | button
    confirm_flags = t_flags | button  # hard breach OR manual press = confirmed

    events = find_events(
        rows, features, combined, confirm_flags=confirm_flags,
        min_duration=min_duration, min_unconfirmed=min_unconfirmed,
        merge_gap=merge_gap, feature_columns=FEATURE_COLUMNS,
    )

    # Attach manual-trigger + coarse fault type, and convert the row-based
    # duration/lead that find_events produced into real seconds at 10 Hz.
    for e in events:
        window = slice(e["start_row"], e["end_row"] + 1)
        e["manual_trigger"] = bool(button[window].any())
        e["trigger"] = infer_trigger(e)
        e["duration_s"] = _duration_s(e)
        if e["confirm_lead_s"]:
            e["confirm_lead_s"] = round(e["confirm_lead_s"] / SAMPLE_HZ, 1)

    breakdown = {
        "threshold": int(t_flags.sum()),
        "isoforest": int(if_flags.sum()),
        "cusum": int(cu_flags.sum()),
        "button": int(button.sum()),
    }
    return events, breakdown


def infer_trigger(event):
    """Coarse fault label from the peak-row snapshot, mirroring
    llm_summary.infer_trigger for the J1939 side. Manual button presses are
    labelled as such only when no sensor bound is breached, so a shake-plus-
    button still reads as a real vibration fault."""
    s = event["signals"]
    if abs(s["accel_mag_ms2"] - GRAVITY) > VIBRATION_DEV_MAX:
        return "vibration"
    if s["temp_c"] > TEMP_C_MAX:
        return "overheat"
    if s["sound_level"] > SOUND_MAX:
        return "loud_acoustic"
    if event.get("manual_trigger"):
        return "manual"
    return "unknown"


# Deterministic templated fallbacks for the summary layer (llm_summary.py),
# keyed by infer_trigger()'s labels. Same role as llm_summary's J1939
# FALLBACK_TEMPLATES: an honest, if blunter, 2-sentence summary (likely fault
# + recommended action) when the LLM is unreachable or too slow. The manual
# template never invents a sensor fault -- it describes an operator-requested
# inspection, matching this piece's button-trigger honesty convention.
FALLBACK_TEMPLATES = {
    "vibration": {
        "cause": (
            "Vibration magnitude of {accel_mag_ms2:.1f} m/s^2 deviates sharply "
            "and continuously from the ~9.8 m/s^2 gravity baseline, consistent "
            "with unbalanced or worn rotating machinery (bearing wear, "
            "imbalance, or a loose mount)."
        ),
        "solution": "Recommend inspecting the drivetrain, bearings, and mounts before continued operation.",
    },
    "overheat": {
        "cause": (
            "Asset temperature of {temp_c:.1f} C exceeds the safe operating "
            "limit, consistent with overheating from cooling loss, sustained "
            "overload, or high ambient heat."
        ),
        "solution": "Recommend reducing load and checking the cooling path before resuming operation.",
    },
    "loud_acoustic": {
        "cause": (
            "Acoustic level of {sound_level:.0f} (mic RMS) has spiked well "
            "above the quiet baseline, consistent with mechanical impact, "
            "cavitation, or a component beginning to fail."
        ),
        "solution": "Recommend an acoustic and physical inspection of the asset.",
    },
    "manual": {
        "cause": (
            "Operator pressed the manual inspection trigger on {asset_id}; the "
            "vibration, temperature, and acoustic signals are within normal bounds."
        ),
        "solution": "Recommend a manual inspection to confirm asset condition, as no automatic fault was detected.",
    },
    "unknown": {
        "cause": (
            "Sensor readings on {asset_id} deviate from baseline (vibration="
            "{accel_mag_ms2:.1f} m/s^2, temp={temp_c:.1f} C, sound={sound_level:.0f})."
        ),
        "solution": "Recommend a manual inspection to confirm the root cause.",
    },
}


def fallback_summary(event):
    """(cause, solution, trigger) deterministic summary for an event -- the
    profile hook llm_summary.py calls when the LLM path is unavailable."""
    trigger = infer_trigger(event)
    fields = dict(event["signals"])
    fields["asset_id"] = event["asset_id"]
    tmpl = FALLBACK_TEMPLATES[trigger]
    return tmpl["cause"].format(**fields), tmpl["solution"].format(**fields), trigger


def format_anomaly_prompt(event):
    """CPX-shaped anomaly prompt for the LLM summary layer -- same structure
    as anomaly_detector.format_anomaly_prompt but for machine-health signals
    off a bench sensor rather than a J1939 engine ECU."""
    lines = [
        f"Anomaly flagged on monitored asset {event['asset_id']} "
        "(vibration/thermal/acoustic sensor pack).",
        "Signal frame:",
    ]
    s = event["signals"]
    flagged = []
    for col in FEATURE_COLUMNS:
        label, unit = CPX_LABELS[col]
        value = s[col]
        flag = ""
        if col == "accel_mag_ms2" and abs(value - GRAVITY) > VIBRATION_DEV_MAX:
            flag = "  <-- ABNORMAL VIBRATION"
        elif col == "temp_c" and value > TEMP_C_MAX:
            flag = "  <-- OVERHEAT"
        elif col == "sound_level" and value > SOUND_MAX:
            flag = "  <-- LOUD"
        if flag:
            flagged.append(label)
        unit_s = f" {unit}" if unit else ""
        lines.append(f"  {label}: {value:.1f}{unit_s}{flag}")

    if event.get("manual_trigger") and not flagged:
        lines.append(
            "Operator pressed the manual inspection trigger; sensor signals "
            "are within normal bounds."
        )
        lines.append("Summarize this as a manual inspection request and the "
                     "recommended check.")
        return "\n".join(lines)

    target = " and ".join(flagged) if flagged else "the deviating signals"
    lines.append(f"Baseline board temperature is ~{BASELINE_TEMP_C:.0f} C at rest.")
    duration_note = f"This has persisted for {event.get('duration_s', 0):.1f}s of continuous readings"
    lead = event.get("confirm_lead_s")
    if lead:
        duration_note += f", statistically flagged {lead:.1f}s before the hard threshold was confirmed"
    lines.append(duration_note + " -- not a single momentary blip.")
    lines.append(f"Summarize the likely fault behind {target} and the "
                 "recommended action.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Detect anomalies in a CPX sensor CSV (live hardware path)."
    )
    ap.add_argument("--csv", default="data/cpx_live_run.csv")
    ap.add_argument("--events-out", default="data/cpx_events.json")
    ap.add_argument("--contamination", type=float, default=0.08)
    ap.add_argument("--min-duration", type=int, default=3)
    ap.add_argument("--min-unconfirmed", type=int, default=8)
    ap.add_argument("--merge-gap", type=int, default=3)
    args = ap.parse_args()

    rows = load_csv(args.csv)
    if not rows:
        print(f"ERROR: no rows in {args.csv}", file=sys.stderr)
        sys.exit(1)

    events, breakdown = detect(
        rows, contamination=args.contamination, min_duration=args.min_duration,
        min_unconfirmed=args.min_unconfirmed, merge_gap=args.merge_gap,
    )

    print(f"Loaded {len(rows)} CPX frames from {args.csv}")
    print(f"Flagged rows by detector: {breakdown} (of {len(rows)})")
    print(f"\n{len(events)} anomaly event(s) detected:")
    for e in events:
        tag = "confirmed" if e["confirmed"] else "unconfirmed (statistical)"
        manual = " [manual trigger]" if e["manual_trigger"] else ""
        lead = ""
        if e["confirmed"] and e["confirm_lead_s"]:
            lead = f"  (drift flagged {e['confirm_lead_s']}s before hard breach)"
        print(f"  [{e['start_ts']} -> {e['end_ts']}]  {e['duration_s']}s  "
              f"trigger={e['trigger']}  {tag}{manual}{lead}")

    with open(args.events_out, "w") as f:
        json.dump(events, f, indent=2)
    print(f"\nWrote {len(events)} event(s) to {args.events_out}")

    if events:
        print("\nSample LLM prompt for the first detected event:")
        print("  " + format_anomaly_prompt(events[0]).replace("\n", "\n  "))


if __name__ == "__main__":
    main()
