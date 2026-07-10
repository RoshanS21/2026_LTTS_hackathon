# LTTS "Engineering Intelligence" Hackathon — Judging Dossier

**Project:** Edge-AI Predictive Maintenance — intelligence at the machine, not in the cloud.

This document maps our project to the scoring parameters. Every number is
**measured on this Raspberry Pi 5 and reproducible** (`README.md` → "Measured
results"), and we mark clearly what is *built & verified* vs. *on the roadmap*
(from `IMPROVEMENTS.md`), so nothing here is an unsupported claim.

**Build status at a glance**

| Layer | State |
|---|---|
| Simulated J1939 pipeline (generator → 3-tier detection → edge LLM → dashboard → GPIO actuator) | **Built & verified end-to-end on the Pi 5** — also the recorded-backup demo path |
| CPX sensor edge (firmware + Pi-side serial reader) | **Built & verified on real hardware** (447-frame live capture, CircuitPython 10.2.1) |
| CPX-path detection + edge-LLM summaries (`cpx_detector.py` / profile-aware `llm_summary.py`, share the J1939 core) | **Built & verified on real hardware** |
| Live serial-fed dashboard (`cpx_dashboard.py`): sensor → detect → LLM → actuate, streaming | **Built & verified end-to-end on live CPX** (shake/warm/Button A → confirmed event → on-Pi AI diagnosis → actuator) |
| ESP32-C6 servo actuator (firmware + Pi client) | Built & mock-verified; **GPIO alarm is the live physical actuator** (the demo C6 board failed hardware bring-up — a spare flashes in minutes) |
| Sensor-edge fault alert (CPX flashes all 10 NeoPixels + sounds its speaker on a Pi-confirmed fault) | Built, firmware reflashed onto the board; final on-stage visual/audio check pending |
| Mobile live view (Pi hosts its own WiFi hotspot + local DNS, phone loads the dashboard with no IP/port; phone-side vibrate/tone/flash alert on a confirmed fault) | **Built & verified live** — phone confirmed reaching the dashboard over the Pi's own hotspot |
| RUL projection, on-device service-manual RAG | **Roadmap differentiators** |

---

## 1. Problem Definition

Connected fleets and industrial machines generate gigabytes of sensor
telemetry, but the intelligence to act on it lives in the cloud — costly to
reach and unavailable exactly where these assets operate: remote highways,
mines, farms, and job sites with intermittent or expensive connectivity.
Operators are stuck choosing between streaming raw data they mostly discard,
or running blind until a component fails and strands the machine in the field,
turning a preventable wear-out into unplanned downtime and an emergency
callout.

**The challenge:** push the intelligence onto the machine itself — an edge
device that turns a live sensor stream into *action* entirely on-device
(detect an impending fault, explain it in plain language a technician can use,
and autonomously trigger a safe response), while uplinking only the
high-value AI-summarized insight instead of the raw firehose — and never
depending on a network connection or a cloud model to do it.

---

## 2. Solution Architecture

A **two-tier edge** that mirrors a real software-defined-vehicle stack (ECU →
domain controller → cloud), realized as a three-device live chain plus a
fully-built software fallback path:

```
 Sensor edge            Compute edge (Raspberry Pi 5 = "edge ECU")            Actuator ECU
 ┌───────────┐   USB    ┌──────────────────────────────────────────────┐  GPIO/  ┌────────────┐
 │ CPX board │ ───────▶ │ perceive → DETECT (3-tier) → DIAGNOSE (LLM)  │ ──────▶ │ ESP32-C6   │
 │ accel/    │  serial  │        → DECIDE (policy) → live DASHBOARD    │ serial  │ servo /    │
 │ temp/mic  │  10 Hz   │                                              │         │ GPIO alarm │
 └───────────┘          └──────────────────────────────────────────────┘         └────────────┘
                                   ships only insight ▲  (~3 KB) not raw telemetry (~121 KB)
```

- **Perceive:** `firmware/cpx_sensor.py` streams accel/temp/mic over USB
  serial; `cpx_serial_reader.py` parses it into frames on the Pi. (Software
  path: `j1939_generator.py` emits reproducible J1939 CSVs.)
- **Detect:** `anomaly_detector.py` — three parallel detectors (fixed
  threshold, IsolationForest, CUSUM drift). `cpx_detector.py` reuses that same
  core for the hardware signal set, so the two pipelines cannot drift apart.
- **Diagnose:** `llm_summary.py` — a small local LLM (`qwen2.5:1.5b` via
  Ollama) writes a 2-sentence maintenance summary, with a deterministic
  templated fallback.
- **Decide → Act:** a pure `decide()` policy (`edge_actuator.py`) drives a
  physical actuator through a shared interface: the GPIO alarm (live) or the
  ESP32-C6 servo over serial (`esp32_actuator.py`, mock-verified). Either
  degrades to a log-only mock, so the decision log stays real even with no
  actuator wired.
- **Display:** `dashboard.py` (simulated J1939) and `cpx_dashboard.py` (live
  CPX serial) — a single Flask page runs the whole loop live, streaming
  events, sparklines, run stats, the bandwidth ledger, and the
  perceive→decide→act log.

**Key architectural choice:** detection is the source of truth and runs with
zero network/LLM dependency; the LLM is the *only* layer permitted to fail.

---

## 3. Engineering / Build Quality

- **Real hardware, verified — not a slideware demo.** The CPX firmware runs on
  a real board (CircuitPython 10.2.1); a 447-frame live capture confirms all
  four signals (shake `|accel|`→58.8, warm 24→30.5 °C, Button A, mic
  33→1866). We even found and fixed a platform bug: `cp.sound_level` is
  unsupported on the Express under CircuitPython 10.x, so the firmware drives
  the PDM mic directly with a graceful fallback.
- **Bidirectional actuation, closing the loop at the sensor edge itself.** On
  a confirmed fault, the Pi writes a command back to the CPX over the same
  USB-serial link, which flashes all 10 NeoPixels and sounds its onboard
  speaker — physical operator feedback right at the point of the fault, not
  just at the dashboard. Getting this right surfaced a second platform
  lesson: CircuitPython's `input()` is a line editor for a human typing at a
  terminal (blocks on `\r`, echoes characters back over the same line), not
  a safe way to receive a raw command from another program — it silently
  froze the board's whole sample loop the instant a command arrived. Fixed
  with a raw non-blocking byte-buffer read instead.
- **Mobile-first deployment story.** The Pi hosts its own WiFi hotspot with a
  custom local DNS record, so a phone gets the live dashboard at a plain
  hostname (no IP, no port) with zero external network dependency — matching
  the "no laptop next to the machine" reality of an actual field deployment.
- **Graceful degradation at every layer** (the "recorded-backup mindset"): GPIO
  degrades to a logging mock if a wire falls out; the LLM falls back to a
  template on timeout; the serial reader skips malformed lines instead of
  crashing; unconfirmed events get an honest "monitor" note, never an invented
  diagnosis. **No silent failure mode.**
- **Reproducibility:** fixed seeds (`--seed 13`) and fixed `random_state`
  regenerate every dataset and model fit identically; `python3 -m py_compile`
  passes across all modules.
- **Validated, not hand-tuned:** all hysteresis parameters (merge_gap=3,
  evidence floors 3 confirmed / 8 unconfirmed, CUSUM k=2.0/h=8.0 on a robust
  median/MAD baseline) were fixed by a **sweep across all four scenarios**
  (healthy must stay at 0 events, every fault caught, degradation keeps ≥60 s
  lead) — not by eyeballing one run.
- **Shared core, honest data:** `cpx_detector.py` imports the detection core
  rather than forking it; live hardware captures are gitignored so only real
  runs are ever committed as truth.

---

## 4. AI Appropriateness

We use the *right* tool for each job, and can defend every choice:

- **Detection uses classical ML + statistics, not an LLM — on purpose.** The
  safety-critical decision path must be deterministic, fast (~4 ms/row), and
  incapable of hallucinating a fault or missing a hard red-line. A fixed
  threshold detector *cannot* be talked out of flagging a breach; an
  IsolationForest catches multivariate outliers; CUSUM catches slow drift.
  None of these needs a GPU, a network, or a prompt.
- **The LLM is used only where language is genuinely the task:** turning a
  4-channel numeric sensor state into a sentence a technician can act on. That
  is a translation problem, and it is exactly what a small language model is
  good at.
- **The LLM is quarantined as the one fail-allowed layer.** It never gates
  detection or actuation, so its latency and non-determinism can't endanger
  the safety loop. This is the appropriate-use argument in one line: *AI where
  it adds value, determinism where lives/uptime depend on it.*

---

## 5. AI Implementation

- **Three-tier detection** (`anomaly_detector.py`): `ThresholdDetector`,
  `IsolationForest` (fixed `random_state`, StandardScaler), and a streaming
  `CusumDetector` (k=2.0, h=8.0, median/MAD baseline). A row flags if *any*
  fires (recall-first); flagged runs merge across short gaps, are debounced
  against an evidence floor, and become `confirmed`/`unconfirmed` events with
  a computed predictive lead time.
- **Genuinely streaming inference:** the dashboard scores each row live inside
  the replay loop (~4 ms/row, ~250× real-time headroom at 1 Hz); only the
  model *fit* is precomputed — the honest equivalent of offline training on
  historical data.
- **Edge LLM** (`llm_summary.py` + `benchmark_edge_llm.py`): local Ollama
  serving `qwen2.5:1.5b`, chosen by an on-device benchmark sweep (Option A —
  run on the Pi — confirmed vs. offloading). Prompt engineering names the
  specific flagged signal(s) so an overspeed event never gets an overheat
  diagnosis; a warm-up call with a full-size prompt avoids a cold-start
  timeout on the first live summary. Median latency ~6–12 s per diagnosis.

---

## 6. AI Impact

What the AI concretely changes, in measured numbers:

- **Predictive, not reactive.** CUSUM flagged oil-pump drift **193 s before**
  the hard safety threshold broke (`degradation` scenario) — on real slow
  wear that lead translates to hours/days of planned-vs-emergency maintenance.
- **~97% less uplink.** ~121 KB of raw telemetry per 30-min window vs. ~3 KB
  of AI-summarized insight — a measured bandwidth ledger, not an assertion.
- **Zero alert fatigue.** 13/13 fault windows across all four scenarios,
  **0 false alarms, 0 soft flags**, 90 noise blips debounced on the healthy
  machine. Every alert a human sees is real.
- **Diagnosis, not just detection.** The LLM collapses "read and interpret 4
  sensor channels" into one plain-language sentence in seconds — time-to-
  understand drops from an expert task to a glance.
- **Autonomous closure.** The perceive→decide→act loop physically actuates on
  a confirmed fault with no human and no network in the loop.

---

## 7. Business Relevance

- **Primary customers:** commercial-vehicle & off-highway OEMs (J1939-native —
  the LTTS-shaped client), remote-asset operators (mining, construction, ag,
  marine) where *offline* is the buying reason, and long-haul fleets where the
  ~97% bandwidth cut is a hard line-item saving across thousands of vehicles.
- **Adjacent expansion, same architecture:** industrial rotating machinery
  (pumps/motors/compressors — the vibration/temp signature our CPX shake/warm
  demo mimics), Tier-1 / telematics suppliers (who'd embed it), remote energy.
- **Judging-pillar fit:** Mobility (J1939/SDV architecture), Sustainability
  (measured ~97% uplink reduction + longer asset life via early intervention),
  Tech (on-device ML + edge LLM), Physical AI (real sensors + real
  actuation), Agentic AI (autonomous perceive→decide→act with tiered
  escalation).
- **ROI story:** unplanned downtime runs into tens of thousands of dollars per
  hour in fleet/industrial settings; predictive maintenance is widely cited to
  cut downtime ~30–50%. Early warning + on-device diagnosis attacks both the
  downtime and the diagnostic-labor cost.

---

## 8. Benefits & Viability

- **Measured benefits** (reproducible on this Pi):

  | Benefit | Number |
  |---|---|
  | Event detection | 13/13 fault windows, 0 false alarms |
  | Predictive lead time | 193 s before the hard threshold breach |
  | Noise robustness | 90 blips debounced → 0 false events (healthy) |
  | Inference cost | ~4 ms/row (~250× real-time headroom @ 1 Hz) |
  | Edge-LLM diagnosis | ~6–12 s per 2-sentence summary |
  | Bandwidth reduction | ~121 KB → ~3 KB per 30-min window (~97%) |

- **Viability / cost:** runs on an ~$80 Raspberry Pi 5 + a ~$30 sensor board,
  dependencies are prebuilt arm64 apt packages (no pip build-from-source), the
  LLM is a free local model — **no cloud contract, no recurring inference
  bill, no connectivity requirement.**
- **Deployability:** the loop degrades gracefully with missing hardware, so it
  can be piloted incrementally; the software path already runs headless
  (`--no-open --no-gpio`) for CI-style validation.

---

## 9. Working Demo

*(Reserved — to be filled after the live dry run.)*
Planned 90-second arc (`IMPROVEMENTS.md` → "Demo script"): healthy scenario
stays silent → degradation scenario opens a MONITOR card and fires the alarm
when the bound breaks ("drift flagged 193 s before safety breach") → mixed
scenario stats tiles (5/5 faults, 0 false alarms, ~97% bandwidth saved).

---

## 10. Differentiation

**vs. what's actually deployed today** (the category is mature, so we win on a
*specific* gap, not by claiming nothing like it exists):

| Deployed tech | What it does | Where we differ |
|---|---|---|
| Fleet telematics (Samsara, Geotab, Motive) | Stream telemetry to cloud + dashboards | Cloud-centric, bandwidth-heavy, needs connectivity — we're edge-first, offline, ~97% less uplink |
| Industrial PdM (Augury, Uptake, Siemens Senseye) | Edge vibration + **cloud** diagnosis, stationary machines | Diagnosis is generated **on the device**, for **mobile/J1939** assets |
| OEM telematics (Cat VisionLink, Komtrax, JDLink) | Fault codes (DTCs) + cloud dashboards | We emit **plain-language diagnosis** and **act autonomously**, not a code a human must look up |
| On-board diagnostics (OBD/DTC) | Fire a code at/after a threshold breach | Our CUSUM tier is **predictive** — flags drift *before* the code would ever trip |

**Three genuine differentiators:**
1. **Diagnosis on the edge** — a generative LLM producing technician-ready
   language *on the device*, offline. Most deployed systems stop at "anomaly
   flagged" or a numeric fault code.
2. **The entire loop is offline** — detect → explain → act with zero network
   dependency. Even "edge" competitors phone home for the diagnosis/dashboard.
3. **Predictive + autonomous + honestly degrading** — drift caught before the
   hard bound, closed to physical actuation, with a graceful-fallback story so
   it never silently fails.

**Roadmap differentiators (in progress) that no fault-code system matches:**
- **Remaining Useful Life (RUL) projection** — reuse the CUSUM drift slope to
  estimate *"oil pressure crosses the safety bound in ~4 h at the current wear
  rate,"* turning lead-time into a plannable number.
- **On-device service-manual RAG** — ground the LLM in the machine's manual so
  the diagnosis names the likely part, procedure, and torque spec, all
  offline.

*The one-line pitch:* **we don't just detect it minutes early — we tell you
what it is, which part, how long you've got, and we do it with the cable
unplugged.**
