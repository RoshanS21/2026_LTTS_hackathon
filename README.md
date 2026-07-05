# Edge-AI Predictive Maintenance — LTTS "Engineering Intelligence" Hackathon

An edge-AI predictive maintenance demo for fleet/equipment telematics. A
Raspberry Pi 5 acts as an "edge ECU": it streams J1939 sensor signals, runs
anomaly detection on-device row-by-row, produces a 2-sentence natural-language
maintenance summary via a small local LLM when a fault fires, and closes a
full perceive → decide → act loop by driving a GPIO alarm (LED/buzzer/relay)
autonomously on confirmed faults. Everything is shown on a live-updating web
dashboard.

Pitch: compute on the edge, ship only high-value AI-summarized insights
instead of raw telemetry — bandwidth/cloud-cost reduction, with the
detection step itself never depending on a network connection or an LLM
being available.

## Measured results (this Pi 5, reproducible from the defaults)

| Claim | Number |
|---|---|
| Fault windows detected (vs. ground truth) | **5/5**, 0 false-alarm events, 19 noise blips debounced |
| Row-level detection (combined OR) | precision 0.87 · recall 0.83 (threshold-only: precision 1.00) |
| Per-row inference cost (threshold + IsolationForest) | ~3.4 ms — real-time at 1 Hz with ~300× headroom |
| Edge-LLM summary latency (`qwen2.5:1.5b`, on-Pi) | **~5.8 s median** per 2-sentence diagnosis |
| Bandwidth: raw J1939 vs. uplinked summaries | 121 KB / 30-min window raw vs. ~2.9 KB of summaries — **~97.6% less uplink** |

Row-level recall < 1.0 is expected and honest: overheat faults ramp in
gradually, so the earliest rows of a fault window are genuinely
indistinguishable from normal. Event-level (5/5) is what a maintenance team
acts on. The dashboard recomputes all of these live on every run.

## Why this design

- **Anomaly detection is deterministic and demo-safe.** The only part of the
  pipeline that's allowed to fail live is the LLM summary — it only adds a
  natural-language gloss on top of a decision already made by a fixed
  threshold + IsolationForest detector, never the fault call itself.
- **Debounced, tiered alerts.** Flagged runs shorter than 3 s are treated as
  sensor noise and suppressed (on this data every real fault persists ≥ 20 s,
  every false positive is a 1–2 s blip). Surviving events are `confirmed`
  (hard safety bound breached → GPIO alarm) or `unconfirmed` (ML-only →
  monitor note, no alarm). A ramping fault escalates live: MONITOR first,
  ALARM the moment a hard threshold breaks — no debounce on safety bounds.
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

## Pipeline

| # | File | What it does |
|---|------|---------------|
| 1 | `benchmark_edge_llm.py` | Benchmarks small Ollama models on this Pi to decide whether the LLM should run on-device (Option A) or offload to a gateway (Option B). |
| 2 | `j1939_generator.py` | Generates a reproducible synthetic J1939 signal CSV with injected anomalies (`low_oil_pressure`, `overheat`, `overspeed`) and ground-truth labels. |
| 3 | `anomaly_detector.py` | Flags anomalies with a fixed-threshold detector (deterministic safety net) and an IsolationForest (catches softer multi-signal drift), debounces sub-3s noise blips, tags each event `confirmed`/unconfirmed, reports row- and event-level metrics, and formats LLM-ready prompts. |
| 4 | `llm_summary.py` | Turns each event into a summary: LLM call for confirmed faults, a plain monitor note for unconfirmed ML-only flags, and a templated fallback if the LLM is unreachable or too slow. |
| 5 | `dashboard.py` | Replays the CSV **row-by-row through live on-device detection** (threshold check + IsolationForest predict per row), streams events to a Flask page as they fire, dispatches LLM summaries in the background, drives the GPIO actuator, and shows live sparklines, run stats, a bandwidth ledger, and the perceive→decide→act log. |
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
sudo apt install -y python3-sklearn python3-flask python3-numpy python3-gpiozero

# Optional hardware: LED (or buzzer/relay module) on GPIO17 + GND.
# Without it everything still runs — the actuator logs its decisions instead.
```

## Usage

```bash
# 1. Decide Option A vs B on this hardware
python3 benchmark_edge_llm.py

# 2. Generate the demo dataset (fixed --seed => reproducible)
python3 j1939_generator.py --out data/demo_run.csv

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

`data/demo_run.csv`, `data/detected_events.json`, and `data/summaries.json`
are committed as a known-good snapshot — the recorded-backup fallback if the
live pipeline can't run on stage. They're fully reproducible from
`j1939_generator.py --seed 13` (the default) plus the piece 3/4 scripts, so
delete and regenerate them any time you want a fresh run.
