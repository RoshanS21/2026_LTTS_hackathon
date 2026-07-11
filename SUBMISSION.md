# LTTS "Engineering Intelligence" Hackathon — Judging Dossier (v2)

**Project:** Edge-AI Retrofit Module — the plug that makes legacy machines AI-smart.

A clamp-on/plug-in module (Pi-5-class "edge ECU" + sensor pod) that retrofits
**intelligence** — not just telemetry — onto machines built decades before AI:
it detects a fault building, explains it in plain language, and acts, all
on-device with zero network dependency. **Flagship target analysis: Republic
Services' waste operation** (trucks, transfer stations, landfill gas/leachate
stations), chosen because every documented pain in that vertical maps to what
this module senses today.

This document maps the project to the scoring parameters. Every technical
number is **measured on this Raspberry Pi 5 and reproducible** (`README.md` →
"Measured results"); every market number is **sourced** (links in §11); and we
mark clearly what is *built & verified* vs. *on the roadmap*
(`IMPROVEMENTS.md`), so nothing here is an unsupported claim.

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
| Gas-sensor pod (H₂S · CH₄ · CO) — the confined-space sentinel config | **Roadmap** — a hardware config on the identical pipeline (same detectors, dashboard, actuation); today's demo-verified sensing is vibration · temp · acoustic · manual trigger |
| RUL projection, on-device service-manual RAG | **Roadmap differentiators** |

**Honesty note on the flagship:** Republic Services is a *target-customer
analysis built from public data* (SEC filings, BLS, Fire Rover, OSHA/NIOSH) —
not a claimed relationship.

---

## 1. Problem Definition

The world runs on machines built before AI — old tractors, refuse trucks,
rigs, compactors — and they cannot say when something is about to go wrong.
Faults, fires and gas build up unnoticed until the last minute, in exactly the
places where connectivity is worst. The waste vertical makes it concrete:

- **America's 5th-deadliest job.** Refuse and recyclable-material collection:
  36 worker deaths in 2024 (BLS); equipment and transport incidents lead the
  causes.
- **The worst fire year on record.** 448 publicly reported waste-facility
  fires in 2025, ~$2.5B in losses — lithium batteries and vapes hiding in
  loads, unseen until they ignite (Fire Rover annual report).
- **Gas that kills the rescuers too.** 46 US workers died from H₂S exposure
  2011–17 (BLS via OSHA); NIOSH case files document a landfill leachate
  station where one worker collapsed and three more died attempting rescue.

Replacing these machines costs six figures each. Today's retrofit options
(cameras, modems, fault codes) all ship data **away** to a cloud to think —
and remote routes, pits and fields are precisely where that link dies.

**The challenge:** retrofit the *intelligence itself* onto the machine — an
add-on edge device that turns a live sensor stream into action entirely
on-device (detect the impending fault, explain it in plain language an
operator can use, autonomously trigger a safe response), uplinking only the
high-value AI-summarized insight, and never depending on a network or a cloud
model to do it.

---

## 2. Solution Architecture

A **retrofit module** in three parts — sense → think → act — realized as a
three-device live chain plus a fully-built software fallback path. It mirrors
a real software-defined-vehicle stack (ECU → domain controller → actuator):

```
 Clamp-on sensor pod       The AI module (Raspberry Pi 5 = "edge ECU")          Actuation & alerting
 ┌────────────────┐  USB   ┌──────────────────────────────────────────────┐  GPIO/  ┌─────────────────┐
 │ vibration/temp │ ─────▶ │ perceive → DETECT (3-tier) → DIAGNOSE (LLM)  │ ──────▶ │ alarm / relay / │
 │ acoustic (CPX) │ serial │        → DECIDE (policy) → live DASHBOARD    │ serial  │ servo + phone   │
 │ gas pod: roadmap│ 10 Hz │          (hosted from its own WiFi hotspot)  │         │ alert, offline  │
 └────────────────┘        └──────────────────────────────────────────────┘         └─────────────────┘
              or tap J1939/CAN directly          ships only insight ▲ (~3 KB) not raw telemetry (~121 KB)
```

- **Perceive:** `firmware/cpx_sensor.py` streams accel/temp/mic over USB
  serial; `cpx_serial_reader.py` parses frames on the Pi. On J1939 machines
  (refuse trucks, tractors) the module can read the bus the machine already
  has — the software path (`j1939_generator.py`) already speaks J1939.
- **Detect:** `anomaly_detector.py` — three parallel detectors (fixed
  threshold, IsolationForest, CUSUM drift). `cpx_detector.py` reuses the same
  core for the hardware signal set, so the two pipelines cannot drift apart.
- **Diagnose:** `llm_summary.py` — a small local LLM (`qwen2.5:1.5b` via
  Ollama) writes a 2-sentence operator summary, with a deterministic templated
  fallback.
- **Decide → Act:** a pure `decide()` policy (`edge_actuator.py`) drives a
  physical actuator through a shared interface: the GPIO alarm (live) or the
  ESP32-C6 servo over serial (`esp32_actuator.py`, mock-verified). Either
  degrades to a log-only mock, so the decision log stays real without
  hardware.
- **Display:** `dashboard.py` (simulated J1939) and `cpx_dashboard.py` (live
  CPX serial) — one Flask page runs the whole loop live, reachable from a
  phone over the module's own hotspot (no IP, no port, no external network).

**Key architectural choice:** detection is the source of truth and runs with
zero network/LLM dependency; the LLM is the *only* layer permitted to fail.
The machine protects itself even when nothing else is reachable.

---

## 3. Engineering / Build Quality

- **Real hardware, verified — not a slideware demo.** The CPX firmware runs on
  a real board (CircuitPython 10.2.1); a 447-frame live capture confirms all
  four signals (shake `|accel|`→58.8, warm 24→30.5 °C, Button A, mic
  33→1866). We found and fixed a platform bug: `cp.sound_level` is
  unsupported on the Express under CircuitPython 10.x, so the firmware drives
  the PDM mic directly with a graceful fallback.
- **Bidirectional actuation, closing the loop at the sensor edge itself.** On
  a confirmed fault, the Pi writes a command back to the CPX over the same
  USB-serial link, which flashes all 10 NeoPixels and sounds its onboard
  speaker — physical operator feedback at the point of the fault. Getting
  this right surfaced a second platform lesson: CircuitPython's `input()` is
  a line editor for a human at a terminal (blocks on `\r`, echoes characters
  back over the same line), not a safe way to receive a raw command — it
  silently froze the board's whole sample loop the instant a command arrived.
  Fixed with a raw non-blocking byte-buffer read.
- **Mobile-first deployment story.** The module hosts its own WiFi hotspot
  with a custom local DNS record, so a phone gets the live dashboard at a
  plain hostname (no IP, no port) with zero external network dependency —
  matching the "no laptop next to the machine" reality of a garage, a tip
  floor, or a field.
- **Graceful degradation at every layer:** GPIO degrades to a logging mock if
  a wire falls out; the LLM falls back to a template on timeout; the serial
  reader skips malformed lines instead of crashing; unconfirmed events get an
  honest "monitor" note, never an invented diagnosis. **No silent failure
  mode.**
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
  4-channel numeric sensor state into a sentence an operator can act on. That
  is a translation problem, and it is exactly what a small language model is
  good at.
- **The LLM is quarantined as the one fail-allowed layer.** It never gates
  detection or actuation: `decide()` acts on the *confirmed sensor event*,
  not on the model's words. A wrong LLM sentence is an advisory-text error,
  never a safety event — it cannot miss a fault, cannot trigger or suppress
  the actuator, and the real signal values and lead time are displayed right
  beside the prose to contradict it. On error/timeout it degrades to a
  deterministic template. *AI where it adds value, determinism where lives
  and uptime depend on it.*

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
- **Edge LLM chosen by measurement, not reputation**
  (`benchmark_edge_llm.py` + `llm_summary.py`): local Ollama serving
  `qwen2.5:1.5b`, selected by an on-device benchmark sweep against a 10 s
  cutoff on the exact anomaly→summary prompt (qwen2.5:0.5b/1.5b, llama3.2:1b,
  gemma2:2b pass; llama3.2:3b does not — Option A, run on the Pi, confirmed
  vs. offloading). Prompt engineering names the specific flagged signal(s) so
  an overspeed event never gets an overheat diagnosis; a warm-up call with a
  full-size prompt avoids a cold-start timeout. Median latency ~6–12 s per
  diagnosis.

---

## 6. AI Impact

What the AI concretely changes, in measured numbers — translated to the
flagship customer's day:

- **Predictive, not reactive.** CUSUM flagged pump drift **193 s before** the
  hard safety threshold broke (`degradation` scenario). On a refuse truck's
  packer hydraulics, that lead is the difference between finishing the route
  into a planned bay and a roadside tow.
- **~97% less uplink.** ~121 KB of raw telemetry per 30-min window vs. ~3 KB
  of AI-summarized insight — a measured bandwidth ledger that makes
  cellular-poor rural routes and remote stations viable with no new
  connectivity spend.
- **Zero alert fatigue.** 13/13 fault windows across all four scenarios,
  **0 false alarms, 0 soft flags**, 90 noise blips debounced on the healthy
  machine. Every alert an operator sees is real — which is what keeps drivers
  from muting the thing.
- **Diagnosis, not just detection.** The LLM collapses "read and interpret 4
  sensor channels" into one plain-language sentence in seconds — time-to-
  understand drops from an expert task to a glance.
- **Autonomous closure.** The perceive→decide→act loop physically actuates on
  a confirmed fault with no human and no network in the loop — a thermal ramp
  in a load raises the alarm *before* the tip floor, not after.

---

## 7. Business Relevance

- **Flagship target: Republic Services** (target-customer analysis from
  public data). ~17,800 collection vehicles (avg age 7.3–8.8 years,
  FY2025 SEC filing) operating in America's 5th-deadliest occupation, through
  the industry's worst fire year on record, with documented H₂S
  multi-fatality events at exactly the stations we'd instrument. Incumbent
  tech (Heil/3rd Eye Connected Collections, McNeilus NGEN) is camera +
  fault-code telemetry, diagnosed in the cloud, tied to their own truck
  bodies. **One module, three deployment surfaces:**
  1. *Collection trucks* — compactor/hydraulic drift caught before the fault
     code, via the J1939 bus the truck already has;
  2. *Transfer stations & MRFs* — thermal precursors on tip floors, conveyors
     and loads before ignition;
  3. *Landfill gas & leachate stations* — the offline H₂S sentinel (gas pod,
     roadmap) for pits and confined spaces.
- **Expansion, same unchanged module:** WM (~2× the trucks) → municipal
  fleets → **the legacy farm fleet** (US tractors run for decades; precision
  ag ships on new iron; autonomy retrofit kits cost $50k–150k — we are the
  ~$110 *awareness* layer for old tractors, where Deere's own JDLink M-Modem
  proves retrofit demand but only uplinks telemetry) → oil & gas / sea rigs /
  remote energy.
- **Judging-pillar fit:** Mobility (J1939/SDV architecture), Sustainability
  (measured ~97% uplink reduction + longer asset life via early
  intervention), Tech (on-device ML + edge LLM), Physical AI (real sensors +
  real actuation), Agentic AI (autonomous perceive→decide→act with tiered
  escalation).

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

- **The economics, per the flagship:** ~$110 of hardware per machine vs. a
  single facility fire event costing $500k to tens of millions (2025
  industry total: ~$2.5B); a roadside tow plus downtime (tens of $k/hour)
  costs more than instrumenting a whole garage. No cloud contract, no
  recurring inference bill, no connectivity requirement — dependencies are
  prebuilt arm64 apt packages and a free local model.
- **The pilot we'd pitch:** 90 days · 1 garage · 50 trucks · 1 transfer
  station. KPIs: faults caught before the DTC ever fires; false-alarm rate
  (ours today: 0 across 13/13 windows); avoided tows and unplanned downtime
  hours; thermal precursor alerts on the tip floor.
- **Deployability:** the loop degrades gracefully with missing hardware, so
  it can be piloted incrementally; the software path already runs headless
  (`--no-open --no-gpio`) for CI-style validation.

---

## 9. Working Demo

**The 90-second arc — "one shift with the module"** (same detection core,
same dashboard, same hardware; only the machine around it changes):

1. **06:12 — packer hydraulics start to drift** *(live: the shake test).*
   CUSUM flags slow wear long before any code would trip — 193 s of lead in
   the measured run. A MONITOR card opens; the truck finishes the route into
   a planned bay, not onto a tow hook.
2. **09:40 — heat ramp in the hopper** *(live: the warming test).* A battery
   in the load starts cooking; the thermal bound breaks → confirmed ALARM:
   on-Pi AI diagnosis, GPIO alarm fires, the CPX flashes and sounds at the
   sensor, the phone on the module's hotspot vibrates — before the tip
   floor, not after.
3. **13:05 — worker check-in at the leachate station** *(live: Button A).*
   The manual trigger opens an inspection event, honestly labeled as manual —
   never blended into sensor data. With the roadmap gas pod, this station
   becomes the H₂S sentinel whose alert goes out *before* the rescuer goes
   in.

Fallback: the fully-built simulated J1939 path replays the recorded-backup
scenarios (`healthy` stays silent → `degradation` opens MONITOR then ALARM →
mixed-scenario stat tiles: 5/5 faults, 0 false alarms, ~97% bandwidth saved).

---

## 10. Differentiation

**They retrofit telemetry. We retrofit intelligence.** (The category is
mature, so we win on a *specific* gap — verified against the actual
incumbents in the flagship vertical.)

| Deployed retrofit tech | What it does | Where we differ |
|---|---|---|
| Heil / 3rd Eye "Connected Collections" | Cameras + engine DTCs → cloud analytics, tied to Heil bodies | Diagnosis **on the device** · body-agnostic · works offline |
| McNeilus NGEN | Advanced diagnostics on their own new bodies | Retrofits **any make or age** — including the old fleet |
| John Deere JDLink M-Modem | Uplinks location & machine data from legacy tractors to the cloud | **Intelligence on the machine** — works with zero coverage |
| OBD / DTC fault codes | A code fires at/after the threshold breach | Our CUSUM tier is **predictive** — flags drift before the code exists |

**Three genuine differentiators:**
1. **Thinks on the machine** — a generative LLM producing operator-ready
   language *on the device*, offline.
2. **Acts on the machine** — detect → explain → physically act with zero
   network dependency. Even "edge" competitors phone home to diagnose.
3. **Predicts before the code** — drift caught before the hard bound, with a
   graceful-fallback story so it never silently fails.

**Roadmap differentiators (clearly separated from what's built):**
- **Gas-sensor pod (H₂S · CH₄ · CO)** — the confined-space sentinel for
  leachate stations, pits and transfer floors: the alert that goes out before
  the rescuer goes in. A hardware config on the *identical* pipeline — same
  detectors, same dashboard, same actuation.
- **Remaining Useful Life (RUL) projection** — reuse the CUSUM drift slope to
  estimate *"hydraulic pressure crosses the safety bound in ~4 h at the
  current wear rate,"* turning lead-time into a plannable number.
- **On-device service-manual RAG** — ground the LLM in the machine's manual
  so the diagnosis names the likely part, procedure, and torque spec, all
  offline.

*The one-line pitch:* **Old iron, new brain — Republic's 17,800 trucks get AI
without buying a single new one, and no one learns about the gas, the fire,
or the failure at the last minute.**

---

## 11. Sources (market & safety numbers)

- Republic Services fleet size & age: FY2025 Annual Report (SEC EDGAR) —
  https://www.sec.gov/Archives/edgar/data/1060391/000119312526121819/d52454dars.pdf
- Fatality ranking, 36 deaths 2024: BLS Census of Fatal Occupational
  Injuries via Waste Dive —
  https://www.wastedive.com/news/bls-fatality-rate-data-2024-waste-collection-/812626/
- 448 facility fires / ~$2.5B losses (2025): Fire Rover annual report via
  Resource Recycling —
  https://resource-recycling.com/e-scrap/2026/03/27/report-pegs-fire-losses-at-2-5b-in-us-and-canada-recycling-industry/
- H₂S: 46 worker deaths 2011–17 (BLS) — https://www.osha.gov/hydrogen-sulfide/hydrogen-sulfide-workplaces ;
  multi-fatality leachate/confined-space cases (NIOSH FACE) —
  https://stacks.cdc.gov/view/cdc/165151
- Heil/3rd Eye Connected Collections — https://www.heil.com/connected-collections/ ,
  https://www.3rdeyecam.com/news/optim-eyes-fleet-maintenance-software/
- Deere JDLink / M-Modem — https://www.deere.com/en/technology-products/precision-ag-technology/data-management/jdlink/
- Ag retrofit-kit pricing ($50k–150k) & market context — Mordor Intelligence,
  https://www.mordorintelligence.com/industry-reports/north-america-agricultural-tractor-machinery-market
- Precision-ag adoption by farm size — USDA ERS,
  https://www.ers.usda.gov/data-products/charts-of-note/110550
