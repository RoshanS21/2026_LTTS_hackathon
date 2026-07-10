# Edge-AI Predictive Maintenance — LTTS "Engineering Intelligence" Hackathon

An edge-AI predictive maintenance demo for fleet/equipment telematics. A
Raspberry Pi 5 acts as an "edge ECU": it streams sensor signals, runs
anomaly detection on-device row-by-row, produces a 2-sentence natural-language
maintenance summary via a small local LLM when a fault fires, and closes a
full perceive → decide → act loop by driving a physical actuator autonomously
on confirmed faults. Everything is shown on a live-updating web dashboard.

Pitch: compute on the edge, ship only high-value AI-summarized insights
instead of raw telemetry — bandwidth/cloud-cost reduction, with the
detection step itself never depending on a network connection or an LLM
being available.

## Two demo paths: live hardware + a recorded-backup software path

We're mid-pivot from a fully simulated pipeline to real physical hardware
(three devices: a Circuit Playground Express sensor edge, the Pi running
detection + LLM + dashboard, an ESP32-C6 driving a servo). **This is the
live, on-stage story.** Pieces are being built in order — see
`firmware/README.md` for what's done.

The original all-software pipeline (`j1939_generator.py` → synthetic J1939
CSVs → `anomaly_detector.py` → `llm_summary.py` → `dashboard.py` →
`edge_actuator.py` driving a GPIO alarm) is fully built, measured, and still
committed below. Per this project's own "recorded-backup mindset" (see
below), it now serves double duty as **the fallback demo path** if the live
hardware chain hits a hiccup on stage — everything in the rest of this
README still runs exactly as documented.

## Measured results (this Pi 5, reproducible from the defaults)

| Claim | Number |
|---|---|
| Fault windows detected (all 4 scenarios combined) | **13/13**, 0 false alarms, 0 soft flags |
| Predictive lead time (slow-decline scenario) | drift flagged **193 s before** the hard safety threshold broke |
| Noise robustness (healthy scenario) | 0 events, 0 alarms; 90 noise blips debounced, injected sensor glitches ignored |
| Per-row inference cost (threshold + IsolationForest + CUSUM) | ~4 ms — real-time at 1 Hz with ~250× headroom |
| Edge-LLM summary latency (`qwen2.5:1.5b`, on-Pi) | **~6–12 s** per 2-sentence diagnosis |
| Bandwidth: raw J1939 vs. uplinked summaries | ~121 KB / 30-min window raw vs. ~3 KB of summaries — **~97% less uplink** |

Row-level recall is deliberately not the headline (0.78–0.83 depending on
scenario): faults ramp in gradually, so the earliest rows of a fault window
are genuinely indistinguishable from normal. Event-level detection is what a
maintenance team acts on. The dashboard recomputes all of these live on
every run.

## Demo scenarios

Four datasets, four stories — pick one with `--csv`:

| Scenario | Command | What it shows |
|---|---|---|
| **mixed** (default) | `python3 dashboard.py` | 5 hard faults across all three types; 5/5 caught, LLM diagnosis per fault, GPIO alarm per fault. |
| **degradation** | `python3 dashboard.py --csv data/scenarios/degradation.csv` | The predictive-maintenance crown jewel: a slow oil-pump-wear drift. Watch the event open as MONITOR, escalate to a GPIO ALARM when the hard bound finally breaks, and the card report *"drift flagged 193 s before safety breach."* |
| **healthy** | `python3 dashboard.py --csv data/scenarios/healthy.csv` | A healthy machine with injected 1–2 s sensor glitches: zero events, zero alarms, zero LLM calls, 100% of telemetry kept on-device. The system knows when to shut up. |
| **stress** | `python3 dashboard.py --csv data/scenarios/stress.csv` | 7 faults + 5 glitches in 30 min: every fault caught, every glitch debounced, the edge LLM keeps up. |

All four are committed under `data/`; regenerate any time with
`python3 j1939_generator.py --all` (fixed seed, fully reproducible).

## Why this design

- **Three detection tiers, each covering what the others miss.** A fixed
  threshold detector (deterministic safety net — cannot miss a hard red-line
  no matter what the models do), an IsolationForest (multivariate outliers),
  and a CUSUM drift detector (classic industrial change detection — catches
  slow degradation like pump wear *minutes* before any hard bound breaks,
  because small persistent deviations accumulate while sensor noise never
  sustains). The LLM only adds a natural-language gloss on top of decisions
  already made — it is the one part of the pipeline allowed to fail live.
- **Debounced, tiered alerts.** Flagged runs merge across short clear gaps
  (one flickering fault ≠ five cards) and need an evidence floor of flagged
  rows before becoming an event — a low bar for threshold-confirmed events,
  a higher one for statistical-only claims. Surviving events are `confirmed`
  (hard safety bound breached → GPIO alarm) or `unconfirmed` (statistical →
  monitor note, no alarm). A drifting fault escalates live: MONITOR first,
  ALARM the moment a hard threshold breaks — safety bounds get no debounce —
  and the card reports the predictive lead time. All hysteresis parameters
  were validated by a sweep across all four scenarios (healthy must stay at
  zero events, every fault must be caught, degradation must keep ≥60 s lead).
- **A real perceive → decide → act loop.** `edge_actuator.py`'s policy is a
  pure function from event → action; the GPIO backend degrades to a mock
  that still logs every decision, so the agentic loop is judge-visible on
  the dashboard even if the LED wire falls out mid-demo.
- **Two-tier edge.** Sensor-edge (the Pi, detection) and compute-edge (the
  Pi or a gateway machine, LLM summary) mirrors a real SDV architecture (ECU
  → domain controller → cloud).
- **Recorded-backup mindset.** Every layer degrades gracefully: unconfirmed
  ML-only alerts get an honest "monitor" note instead of an invented
  diagnosis, and if the LLM is slow/unreachable, a deterministic templated
  summary takes over — the demo has no silent failure mode.

## Live hardware pipeline (in progress)

Three physical devices: a Circuit Playground Express (CPX) sensor edge, the
Raspberry Pi 5 (detection + LLM + dashboard), and an ESP32-C6 actuator ECU
driving a servo (optionally reading a 5V Wiegand badge reader for
identity/audit — strictly optional, never gates the core loop).

| # | File | What it does |
|---|------|---------------|
| 1a | `firmware/cpx_sensor.py` | CircuitPython firmware for the CPX: streams onboard accelerometer/temperature/mic-loudness as clean CSV lines over USB serial at 10Hz. A fault is physically inducible — shake the board (accel spike) or warm it (temp rise). Button A is a transparent manual-trigger flag for demo safety, reported as-is, never blended into sensor values. See `firmware/README.md` for install + on-stage notes. |
| 1b | `cpx_serial_reader.py` | Pi-side reader: auto-detects the CPX by USB VID, parses its serial CSV into frame dicts (assigns the wall-clock timestamp the CPX itself can't), skips malformed lines with a warning instead of crashing, and appends every live run to a CSV for the recorded-backup pile. `--mock` generates synthetic frames (with an optional injected shake burst) so downstream pieces can be built/tested without hardware attached. |
| 2 | *(next)* | Anomaly detection adapted to consume live CPX frames instead of J1939 CSV rows. |
| 3 | *(next)* | LLM summary layer — reuse `llm_summary.py`'s prompt/fallback pattern against the new signal set. |
| 4 | *(next)* | ESP32-C6 servo firmware (+ optional Wiegand reader, level-shifted). |
| 5 | *(next)* | Flask dashboard adapted for the live serial-fed pipeline. |

## Software pipeline (recorded-backup path)

| # | File | What it does |
|---|------|---------------|
| 1 | `benchmark_edge_llm.py` | Benchmarks small Ollama models on this Pi to decide whether the LLM should run on-device (Option A) or offload to a gateway (Option B). |
| 2 | `j1939_generator.py` | Generates reproducible synthetic J1939 CSVs by scenario (`mixed`/`healthy`/`degradation`/`stress`) with injected hard faults, a slow-drift fault, benign sensor glitches, and ground-truth labels. |
| 3 | `anomaly_detector.py` | Flags anomalies with three parallel detectors (fixed thresholds, IsolationForest, streaming CUSUM), merges/debounces flagged runs, tags each event `confirmed`/unconfirmed with predictive lead time, reports row- and event-level metrics, and formats LLM-ready prompts. |
| 4 | `llm_summary.py` | Turns each event into a summary: LLM call for confirmed faults, a plain monitor note for unconfirmed ML-only flags, and a templated fallback if the LLM is unreachable or too slow. |
| 5 | `dashboard.py` | Replays any scenario CSV **row-by-row through live on-device detection** (threshold check + IsolationForest predict + incremental CUSUM step per row), streams events to a Flask page as they fire, dispatches LLM summaries in the background, drives the GPIO actuator, and shows live sparklines, run stats, a bandwidth ledger, and the perceive→decide→act log. |
| 6 | `edge_actuator.py` | The "act" step: a pure decision policy plus a GPIO alarm (LED/buzzer/relay on GPIO17 by default) with a mock fallback and a judge-visible action log. `--test` gives a 3-pulse hardware self-test. |

## Decisions already made (see each script's docstring for detail)

- **Option A confirmed**: on this Pi 5, `qwen2.5:0.5b`/`1.5b`, `llama3.2:1b`,
  and `gemma2:2b` all summarize in well under the 10s cutoff (`llama3.2:3b`
  does not). Default model is `qwen2.5:1.5b` — a speed/quality sweet spot.
  The warm-up uses a full-size anomaly prompt so the first live summary
  doesn't pay cold prompt-eval costs.
- **Default seed is 13**: it injects all three fault types (2× low oil
  pressure, 2× overheat, 1× overspeed) spread across the 30-min run, so the
  demo shows fault-type variety. (Seed 42 happens to give 5× low oil
  pressure.)
- Judging-pillar fit: Mobility (J1939/SDV architecture), Sustainability
  (measured ~97.6% uplink reduction), Tech (on-device ML + edge LLM),
  Physical AI (GPIO actuation on real hardware), **Agentic AI (autonomous
  perceive→decide→act with a tiered escalation policy)**.
- NFC tap (PN532) hardware step was descoped in favor of the dashboard
  auto-opening a browser tab directly, to keep the demo simpler and avoid
  iOS/Android NFC-launch inconsistency.

## Setup (fresh Pi)

```bash
# Ollama + models (see benchmark_edge_llm.py for the full model sweep)
ollama pull qwen2.5:1.5b

# System packages (prebuilt arm64 wheels via apt — much faster than pip build-from-source on a Pi)
sudo apt install -y python3-sklearn python3-flask python3-numpy python3-gpiozero python3-serial

# Optional hardware: LED (or buzzer/relay module) on GPIO17 + GND.
# Without it everything still runs — the actuator logs its decisions instead.

# CPX: flash firmware/cpx_sensor.py per firmware/README.md, then plug into the Pi over USB.
```

## Usage

```bash
# 0. CPX live sensor stream (auto-detects the board's serial port)
python3 cpx_serial_reader.py
python3 cpx_serial_reader.py --mock --fault-at 5 --fault-duration 4   # no hardware -- synthetic test

# 1. Decide Option A vs B on this hardware
python3 benchmark_edge_llm.py

# 2. Generate the demo datasets (fixed --seed => reproducible)
python3 j1939_generator.py --all

# 3. Run detection standalone (prints row- and event-level metrics vs. ground truth)
python3 anomaly_detector.py --csv data/demo_run.csv

# 4. Run detection + LLM summary standalone
python3 llm_summary.py --csv data/demo_run.csv

# 5. Hardware self-test: 3 alarm pulses on GPIO17
python3 edge_actuator.py --test

# 6. Run the full live demo (run this one on the Pi's own desktop session,
#    not over a plain SSH shell, so the browser tab has a display to open into).
#    Default replay is 30x: the 30-min dataset plays in ~60s.
python3 dashboard.py
python3 dashboard.py --speed 60 --no-open --no-gpio   # headless / no hardware
```

## Data

`data/demo_run.csv`, `data/scenarios/*.csv`, `data/detected_events.json`,
and `data/summaries.json` are committed as a known-good snapshot — the
recorded-backup fallback if the live pipeline can't run on stage. They're
fully reproducible from `j1939_generator.py --all` (fixed default seed)
plus the piece 3/4 scripts, so delete and regenerate them any time you want
a fresh run.
