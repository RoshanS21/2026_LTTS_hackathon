# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Edge-AI predictive-maintenance demo for the LTTS hackathon, running **on the Raspberry Pi 5 itself** (this machine is the demo hardware — an "edge ECU"). Two parallel demo paths share one detection core:

- **Live hardware path (the on-stage story, in progress):** Circuit Playground Express sensor edge → USB serial → Pi (detection + LLM + dashboard) → ESP32-C6 servo actuator. Built pieces: `firmware/cpx_sensor.py` (CPX CircuitPython firmware), `cpx_serial_reader.py` (Pi-side reader), `cpx_detector.py` (CPX signal profile for the detector). Remaining pieces are listed in `README.md` ("Live hardware pipeline").
- **Simulated J1939 path (fully built, the recorded-backup fallback):** `j1939_generator.py` → synthetic CSVs → `anomaly_detector.py` → `llm_summary.py` → `dashboard.py` → `edge_actuator.py` (GPIO17 alarm). This path must keep working exactly as documented in `README.md` — it is the on-stage fallback if the hardware chain hiccups.

## Commands

No build system or test suite; verification is running the scripts (each has a docstring with usage). Syntax check after edits:

```bash
python3 -m py_compile anomaly_detector.py dashboard.py edge_actuator.py llm_summary.py j1939_generator.py benchmark_edge_llm.py cpx_serial_reader.py cpx_detector.py
```

```bash
# Regenerate all demo datasets (fixed seed 13 → reproducible; seed chosen to show all 3 fault types)
python3 j1939_generator.py --all

# Detection standalone, with row-/event-level metrics vs. ground truth
python3 anomaly_detector.py --csv data/demo_run.csv

# Detection + LLM summaries (needs Ollama running locally with qwen2.5:1.5b)
python3 llm_summary.py --csv data/demo_run.csv

# Full demo dashboard — headless flags for dev/CI-style runs:
python3 dashboard.py --no-open --no-gpio --speed 100 --port 5057
# Scenario variants: --csv data/scenarios/{degradation,healthy,stress}.csv

# GPIO self-test (3 alarm pulses on GPIO17); --no-gpio exercises the mock
python3 edge_actuator.py --test

# CPX live stream; --mock develops/tests without hardware attached
python3 cpx_serial_reader.py
python3 cpx_serial_reader.py --mock --fault-at 5 --fault-duration 4

# CPX-path detection on a recorded live run
python3 cpx_detector.py --csv data/cpx_live_run.csv
```

Dependencies come from apt (prebuilt arm64 wheels), not pip: `python3-sklearn python3-flask python3-numpy python3-gpiozero python3-serial`. The LLM is a local Ollama serving `qwen2.5:1.5b` at 127.0.0.1:11434.

## Architecture

**Three-tier detection core, shared by both paths.** `anomaly_detector.py` runs three detectors in parallel — fixed thresholds (deterministic safety net), IsolationForest (multivariate outliers, fixed `random_state`), and CUSUM (slow drift — the genuinely predictive tier). A row flagged by ANY detector enters event forming: runs merge across short clear gaps, get debounced against an evidence floor, and become `confirmed` (hard bound breached → ALARM) or `unconfirmed` (statistical only → MONITOR, no alarm) events with predictive lead time. `cpx_detector.py` supplies only the CPX-specific signal profile (columns, bounds, fault semantics, prompt) and **imports the core from `anomaly_detector.py`** — keep it that way so the two pipelines can't drift apart. Hysteresis parameters were validated by a sweep across all four scenarios (healthy must stay at 0 events, every fault caught, degradation keeps ≥60 s lead) — re-run all four scenarios if you touch them.

**Failure-tolerance layering is the design's spine.** Detection is the source of truth and never depends on the network or the LLM. `llm_summary.py` is the *only* layer allowed to fail live: it falls back to a deterministic templated summary on error/timeout, and unconfirmed events get an honest "monitor" note, never an invented diagnosis. `edge_actuator.py` keeps `decide()` a pure function (event → action) separate from actuation, and the GPIO backend degrades to a mock that still logs every decision — the perceive→decide→act log on the dashboard stays real even without hardware.

**Honesty conventions, enforced on purpose:**
- CPX Button A is a manual-trigger flag reported as its own column, never blended into sensor values; events it opens carry `manual_trigger` so the summary describes a manual inspection request, not a fake sensor fault.
- `data/cpx_live_run.csv` is gitignored — only real hardware captures may be committed there, never synthetic stand-ins.
- `data/*.csv/json` committed files are the known-good recorded-backup snapshot; they're fully reproducible (fixed seeds), so regenerate rather than hand-edit.
- Measured numbers in `README.md` ("Measured results" table) were produced on this Pi — if a change affects them, re-measure rather than leaving stale claims.

**Dashboard streaming is real.** `dashboard.py` replays a CSV row-by-row through live per-row detection (~4 ms/row); only the IsolationForest *fit* is precomputed (the honest equivalent of offline training). Don't "optimize" this into batch precomputation.

## Hardware/environment gotchas

- Only one process can hold a serial port — close `screen`/Mu/Thonny before running `cpx_serial_reader.py`. Serial access needs the `dialout` group. See `firmware/PI_TEST_NOTES.md`.
- The CPX auto-detects by Adafruit USB VID 0x239A, usually `/dev/ttyACM0`. Firmware install steps are in `firmware/README.md` (CircuitPython 10.x; `code.py` auto-runs on reset).
- `dashboard.py` opens a browser tab; over plain SSH use `--no-open`. Without wired hardware use `--no-gpio`.
