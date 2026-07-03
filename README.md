# Edge-AI Predictive Maintenance — LTTS "Engineering Intelligence" Hackathon

An edge-AI predictive maintenance demo for fleet/equipment telematics. A
Raspberry Pi 5 acts as an "edge ECU": it streams J1939 sensor signals, runs
deterministic anomaly detection on-device, and when an anomaly fires it
produces a 2-sentence natural-language mechanical maintenance summary via a
small local LLM. Results are shown on a live-updating web dashboard.

Pitch: compute on the edge, ship only high-value AI-summarized insights
instead of raw telemetry — bandwidth/cloud-cost reduction, with the
detection step itself never depending on a network connection or an LLM
being available.

## Why this design

- **Anomaly detection is deterministic and demo-safe.** The only part of the
  pipeline that's allowed to fail live is the LLM summary — it only adds a
  natural-language gloss on top of a decision already made by a fixed
  threshold + IsolationForest detector, never the fault call itself.
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
| 3 | `anomaly_detector.py` | Flags anomalies with a fixed-threshold detector (deterministic safety net) and an IsolationForest (catches softer multi-signal drift), tags each event `confirmed`/unconfirmed, and formats LLM-ready prompts. |
| 4 | `llm_summary.py` | Turns each event into a summary: LLM call for confirmed faults, a plain monitor note for unconfirmed ML-only flags, and a templated fallback if the LLM is unreachable or too slow. |
| 5 | `dashboard.py` | Runs the whole pipeline in the background and serves a live-updating Flask page, auto-opening a browser tab so results appear with nothing to click during the demo. |

## Decisions already made (see each script's docstring for detail)

- **Option A confirmed**: on this Pi 5, `qwen2.5:0.5b`/`1.5b`, `llama3.2:1b`,
  and `gemma2:2b` all summarize in well under the 10s cutoff (`llama3.2:3b`
  does not). Default model is `qwen2.5:1.5b` — a speed/quality sweet spot.
- Judging-pillar fit: strong on Mobility, Sustainability, Tech, and Physical
  AI. Agentic AI is the one weak spot (the LLM only summarizes, it doesn't
  act) — a possible later addition is a small perceive→decide→act step
  (severity classification → auto-drafted ticket), only after the core path
  is solid.
- NFC tap (PN532) hardware step was descoped in favor of the dashboard
  auto-opening a browser tab directly, to keep the demo simpler and avoid
  iOS/Android NFC-launch inconsistency.

## Setup (fresh Pi)

```bash
# Ollama + models (see benchmark_edge_llm.py for the full model sweep)
ollama pull qwen2.5:1.5b

# System packages (prebuilt arm64 wheels via apt — much faster than pip build-from-source on a Pi)
sudo apt install -y python3-sklearn python3-flask python3-numpy
```

## Usage

```bash
# 1. Decide Option A vs B on this hardware
python3 benchmark_edge_llm.py

# 2. Generate the demo dataset (fixed --seed => reproducible)
python3 j1939_generator.py --out data/demo_run.csv

# 3. Run detection standalone (prints precision/recall vs. ground truth)
python3 anomaly_detector.py --csv data/demo_run.csv

# 4. Run detection + LLM summary standalone
python3 llm_summary.py --csv data/demo_run.csv

# 5. Run the full live demo (run this one on the Pi's own desktop session,
#    not over a plain SSH shell, so the browser tab has a display to open into)
python3 dashboard.py
```

## Data

`data/demo_run.csv`, `data/detected_events.json`, and `data/summaries.json`
are committed as a known-good snapshot — the recorded-backup fallback if the
live pipeline can't run on stage. They're fully reproducible from
`j1939_generator.py --seed 42` (the default) plus the piece 3/4 scripts, so
delete and regenerate them any time you want a fresh run.
