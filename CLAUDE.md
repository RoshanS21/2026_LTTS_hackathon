# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Edge-AI predictive-maintenance demo for the LTTS hackathon, running **on the Raspberry Pi 5 itself** (this machine is the demo hardware — an "edge ECU"). Two parallel demo paths share one detection core:

- **Live hardware path (the on-stage story, fully built & verified):** Circuit Playground Express sensor edge → USB serial → Pi (detection + LLM + `cpx_dashboard.py`) → GPIO17 LED / ESP32-C6 servo actuator. Pieces: `firmware/cpx_sensor.py` (CPX CircuitPython firmware — also flashes/tones the board on a Pi-confirmed fault), `cpx_serial_reader.py` (Pi-side reader), `cpx_detector.py` (CPX signal profile), `cpx_dashboard.py` (the live streaming dashboard, also reachable from a phone — see README "Mobile / remote live view"). See `README.md` ("Live hardware pipeline") for full detail.
- **Simulated J1939 path (fully built, the recorded-backup fallback):** `j1939_generator.py` → synthetic CSVs → `anomaly_detector.py` → `llm_summary.py` → `dashboard.py` → `edge_actuator.py` (GPIO17 alarm). This path must keep working exactly as documented in `README.md` — it is the on-stage fallback if the hardware chain hiccups.

## Commands

No build system or test suite; verification is running the scripts (each has a docstring with usage). Syntax check after edits:

```bash
python3 -m py_compile anomaly_detector.py dashboard.py edge_actuator.py llm_summary.py j1939_generator.py benchmark_edge_llm.py cpx_serial_reader.py cpx_detector.py cpx_dashboard.py esp32_actuator.py firmware/cpx_sensor.py
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

# Live CPX hardware dashboard (the on-stage demo)
python3 cpx_dashboard.py --gpio
python3 cpx_dashboard.py --mock --fault-at 10   # rehearse with no hardware
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
- The CPX auto-detects by Adafruit USB VID 0x239A, usually `/dev/ttyACM0`. Firmware install steps are in `firmware/README.md` (CircuitPython 10.x; `code.py` auto-runs on reset). After editing `firmware/cpx_sensor.py`, it must be re-copied onto the mounted `CIRCUITPY` drive as `code.py` to take effect — CircuitPython auto-reloads on write, which resets the board and drops any open serial connection (restart whatever Pi-side process was reading it).
- `dashboard.py` opens a browser tab; over plain SSH use `--no-open`. Without wired hardware use `--no-gpio`.
- **Pi -> CPX command channel:** `cpx_dashboard.py` writes a `FAULT\n` command back over the same serial connection the instant a fault is confirmed, so the CPX can flash its NeoPixels + sound its speaker (both self-timed on the CPX side). The firmware reads this via a raw `supervisor.runtime.serial_bytes_available` + `sys.stdin.read()` byte-buffer check — **not** `input()`, which is a line editor that can block waiting for `\r` and echoes characters back over the same line; that mismatch previously froze the CPX's whole sample loop (and therefore the whole dashboard, since frames stopped arriving) the instant a fault fired.
- **Serial auto-reconnect.** `cpx_dashboard.py`'s `frame_source()` (live branch) wraps the serial read in a retry loop: on any exception it sets `STATE["status"] = "reconnecting"`, sleeps 1.5 s, and reopens the port (re-resolving via `find_cpx_port()` unless `--port` was explicit), forever. This exists because a transient USB/CDC read glitch (confirmed via `dmesg` — the board never actually re-enumerates) previously killed the pipeline thread silently and froze the dashboard on stale data forever, with no self-recovery. Diagnose a stuck dashboard by checking whether `frames` in `/api/state` is still incrementing — if `status` is stuck on `"reconnecting"`, the port itself is unreachable (check `lsof`/`dmesg` for a real disconnect or a competing process).
- **Mobile/phone live view:** the Pi can host its own WiFi hotspot (`sudo nmcli device wifi hotspot ssid timtam password "maintain123"`) with a custom local DNS record (`/etc/NetworkManager/dnsmasq-shared.d/dashboard.conf`: `address=/timtam.box/10.42.0.1`), so a phone joined to that hotspot can browse `http://timtam.box` with no IP and no port — requires running the dashboard with `sudo ... --http-port 80` (or `--port 80` for `dashboard.py`, which uses a different flag name than `cpx_dashboard.py`'s `--http-port`). Needs a second network path (e.g. Ethernet) for your own management access, since the hotspot takes over `wlan0`. See README.md "Mobile / remote live view" for the full recipe.
