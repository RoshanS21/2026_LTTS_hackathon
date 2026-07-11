# Edge-AI Predictive Maintenance — LTTS "Engineering Intelligence" Hackathon

An edge-AI predictive maintenance demo built around one concrete real-world
use case: **GEN-01, a 500 kVA standby diesel generator** in a hospital
basement plant room. A Raspberry Pi 5 acts as the site's "edge ECU": it
streams sensor signals, runs anomaly detection on-device row-by-row, produces
a 2-sentence natural-language maintenance summary via a small local LLM when
a fault fires — personalized to *this* generator, not generic sensor talk —
and closes a full perceive → decide → act loop by driving a physical actuator
autonomously on confirmed faults. Everything is shown on a live-updating web
dashboard.

Pitch: compute on the edge, ship only high-value AI-summarized insights
instead of raw telemetry — bandwidth/cloud-cost reduction, with the
detection step itself never depending on a network connection or an LLM
being available.

## The use case: a standby generator that must never fail quietly

A hospital's backup genset sits idle 99% of the time and is exercised by a
short transfer test at fixed intervals; a fault that develops *between* tests
is discovered exactly when it matters most — during a real outage. That is
the textbook case for continuous edge monitoring, and both demo paths tell
this one story:

- **The J1939 simulated path is GEN-01's own engine ECU.** Real diesel
  gensets speak J1939 natively — SPN 190 engine speed, SPN 110 coolant temp,
  SPN 100 oil pressure, SPN 175 oil temp are exactly the signals this demo
  synthesizes and detects on.
- **The live CPX hardware is a retrofit sensor pod bolted to GEN-01's
  frame** — the low-cost "add monitoring to an old genset" play. Frame
  vibration ⇒ failing engine mount / coupling misalignment / misfire;
  frame overheat ⇒ blocked radiator airflow / coolant loss / overload;
  acoustic spike ⇒ engine knock / belt failure. The AI summaries diagnose in
  those terms and recommend genset-specific actions (e.g. "hold the unit
  from its next transfer test until mounts are inspected").

Honesty note: the persona (name, rating, site — defined once in
`benchmark_edge_llm.py` and imported everywhere) shapes only labels and the
LLM story. Sensor values are always reported exactly as measured, and the
manual Button A trigger is still reported as a technician inspection
request, never dressed up as a sensor fault.

## Quick reference: start / stop the phone-facing dashboard

The one-line way — `start_dashboard.sh` / `stop_dashboard.sh` (repo root),
wrapping everything below: refuses to start if already running or if port
80 / the CPX serial port is already held by something else, backgrounds it
with `nohup` so it survives closing the terminal, and confirms it's
actually streaming (not just that the process exists) before printing the
phone URL:

```bash
./start_dashboard.sh
./stop_dashboard.sh
```

The commands they wrap, for the live CPX dashboard running on port 80 so a
phone can reach it with no port in the URL (full setup/context in "Mobile /
remote live view" below):

```bash
sudo python3 cpx_dashboard.py --gpio --http-port 80
```

- `sudo` — port 80 is a privileged port (<1024), needs root.
- `--http-port 80` — so the phone's URL needs no `:5000` suffix.
- `--gpio` — drives the real GPIO17 LED as the actuator (omit for the
  ESP32-C6 servo path instead).
- add `--no-open` when running headless over SSH (skips trying to open a
  local browser tab).

**Stop it:** if it's running in the foreground (the command above, in a
terminal you're watching), just `Ctrl+C`. If it's backgrounded (e.g.
started with `nohup ... &` so it survives closing the terminal), match on
the script name alone, not the full flag list — there should only ever be
one instance running (the CPX serial port can't be shared), and matching
a specific flag combination is fragile the moment the start command
includes an extra flag like `--no-open` that isn't repeated here:

```bash
sudo pkill -f "cpx_dashboard.py"
```

To run it backgrounded in the first place (useful if you want it to
survive an SSH disconnect):

```bash
sudo nohup python3 cpx_dashboard.py --gpio --no-open --http-port 80 > /tmp/cpx_dashboard.log 2>&1 &
```

Before starting a new instance, always confirm the port and the CPX serial
port are actually free — a leftover process silently holding either one is
the single most common cause of "it won't start" or "device disconnected /
multiple access on port" errors:

```bash
sudo lsof -i :80
sudo lsof /dev/ttyACM0
```

## Two demo paths, both built: live hardware + a recorded-backup software path

**The live hardware story (the on-stage demo).** A Circuit Playground Express
(CPX) sensor edge streams accelerometer/temperature/mic over USB serial to the
Pi, which detects → diagnoses (edge LLM) → decides → actuates, and shows it all
on a live web page: `cpx_serial_reader.py` → `cpx_detector.py` →
`llm_summary.py --profile cpx` → actuator → `cpx_dashboard.py`. This is fully
built and **verified end-to-end on real hardware** — shake/warm the board or
press Button A and a confirmed event fires with an on-Pi AI diagnosis and a
physical actuation. Run it with `python3 cpx_dashboard.py --gpio`.

**The simulated J1939 path (the recorded-backup).** The original all-software
pipeline (`j1939_generator.py` → synthetic J1939 CSVs → `anomaly_detector.py`
→ `llm_summary.py` → `dashboard.py` → `edge_actuator.py` driving a GPIO alarm)
is fully built, measured, and reproducible. Per this project's own
"recorded-backup mindset," it serves double duty as **the fallback demo path**
if the live hardware chain hiccups on stage — everything in the rest of this
README runs exactly as documented.

Both paths **share one detection core** (`anomaly_detector.py`) and one LLM
engine (`llm_summary.py`), so they can't drift apart — the CPX path only adds
its own signal profile.

## Measured results (this Pi 5, reproducible from the defaults)

| Claim | Number |
|---|---|
| Fault windows detected (all 4 scenarios combined) | **13/13**, 0 false alarms, 0 soft flags |
| Predictive lead time (slow-decline scenario) | drift flagged **193 s before** the hard safety threshold broke |
| Noise robustness (healthy scenario) | 0 events, 0 alarms; 91 noise blips debounced, injected sensor glitches ignored |
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
  Pi or a gateway machine, LLM summary) mirrors a real industrial-edge
  architecture (genset engine ECU → site gateway → cloud).
- **Recorded-backup mindset.** Every layer degrades gracefully: unconfirmed
  ML-only alerts get an honest "monitor" note instead of an invented
  diagnosis, and if the LLM is slow/unreachable, a deterministic templated
  summary takes over — the demo has no silent failure mode.

## Live hardware pipeline (built & verified on real hardware)

Three physical devices: a Circuit Playground Express (CPX) sensor edge, the
Raspberry Pi 5 (detection + LLM + dashboard), and an ESP32-C6 actuator ECU
driving a servo.

| # | File | What it does |
|---|------|---------------|
| 1a | `firmware/cpx_sensor.py` | CircuitPython firmware for the CPX: streams onboard accelerometer/temperature/mic-loudness as clean CSV lines over USB serial at 10 Hz. A fault is physically inducible — shake the board (accel spike) or warm it (temp rise). Button A is a transparent manual-trigger flag for demo safety, reported as-is, never blended into sensor values. Drives the PDM mic directly via `audiobusio` (CircuitPython 10.x stubs `cp.sound_level` on the CPX) with a graceful no-mic fallback. Also listens for a `FAULT` command from the Pi (same USB-serial line) and flashes all 10 NeoPixels red + sounds the onboard speaker, both self-timed so a dropped message can't stick. See `firmware/README.md`. |
| 1b | `cpx_serial_reader.py` | Pi-side reader: auto-detects the CPX by USB VID, parses its serial CSV into frame dicts (assigns the wall-clock timestamp the CPX can't), skips malformed lines instead of crashing, and appends every live run to a CSV. `--mock` generates synthetic frames for hardware-free testing. |
| 2 | `cpx_detector.py` | The CPX **signal profile** (vibration-vs-gravity / temp / mic-RMS bounds, Button A as the honest manual-confirm trigger) that **imports the 3-tier detection core from `anomaly_detector.py`** — so the live and simulated pipelines share one detector and can't drift. |
| 3 | `llm_summary.py --profile cpx` | The same edge-LLM engine, made profile-aware: CPX events get vibration/thermal/acoustic prompts + fallback templates (from `cpx_detector.py`); the LLM call, timeout, warm-up, and fallback dispatch stay shared with the J1939 path. |
| 4 | `firmware/esp32c6_actuator/` + `esp32_actuator.py` | ESP32-C6 Arduino servo firmware (serial `ALARM`/`CLEAR`/`PING`, "flagging" sweep) + a Pi-side serial client that reuses the shared `decide()` policy and mirrors `edge_actuator.Actuator`'s interface. Both degrade to a log-only mock. See `firmware/ESP32C6_ACTUATOR.md` (wiring + flashing). |
| 5 | `cpx_dashboard.py` | The live serial-fed dashboard: baseline warm-up (fit the models on ~8 s of at-rest data — the honest train-offline/infer-online split), then per-frame detection → edge-LLM summary → actuator, streaming to a self-updating page with CPX sparklines, event cards, run stats, a bandwidth ledger, and the perceive→decide→act log. On a confirmed fault it also writes the `FAULT` command back to the CPX (LED flash + speaker tone) and fires an in-page phone alert (vibration + tone + screen flash) — see "Mobile / remote live view" below for phone access. |

**Verified live:** a continuous shake produces bounded confirmed vibration
events (~8 s each, force-closed so a persistent offset can't freeze one open),
each with a real on-Pi AI diagnosis (~8 s), the actuator cycling ALARM/CLEAR,
and ~90% bandwidth saved — no monitor-noise swarm.

**CPX fault alert + mobile live view.** On a confirmed fault the CPX flashes
red and sounds its speaker (implemented and reflashed onto the board; final
on-stage visual/audio check still pending). Mobile access is verified live:
the Pi hosts its own WiFi hotspot with a custom local DNS record, so a phone
on that hotspot loads the dashboard at a plain hostname — no IP, no port. See
"Mobile / remote live view" below.

> **ESP32-C6 hardware note.** The demo C6 board failed hardware bring-up (the
> chip stopped presenting on both its native USB and UART after an early servo
> miswire; a spare flashes in minutes with the recipe in
> `firmware/ESP32C6_ACTUATOR.md`). So the **live physical actuator is the GPIO17
> LED** (`cpx_dashboard.py --gpio`), verified working; the servo path is
> mock-verified and drop-in ready. The perceive→decide→act log stays real
> either way.
> **ESP32-C6 hardware note.** The original demo C6 died during bring-up (stopped
> presenting on both native USB and UART after an early servo miswire). A spare
> was flashed in minutes (macOS recipe + gotchas in
> `firmware/ESP32C6_ACTUATOR.md`) and the **full servo path is now
> hardware-verified end to end, physical motion included**: `esp32_actuator.py
> --test` drives the board through the shared `decide()` policy (auto-detected on
> `/dev/cu.usbmodem*`, VID `0x303A`), the servo physically sweeps on each ALARM
> pulse, and unconfirmed events correctly log `MONITOR` with no actuation.
> The one bring-up gotcha worth knowing: **boot the C6 with the servo already
> fully wired and powered — hot-plugging a servo onto a live C6 crashes its USB
> and needs a power cycle to recover** (bisected + wiring in
> `firmware/ESP32C6_ACTUATOR.md`). `cpx_dashboard.py` defaults to the servo
> actuator; `--gpio` swaps in the GPIO17 LED (also verified) as a no-external-
> power fallback. The perceive→decide→act log stays real either way.

## Mobile / remote live view

In a real deployment there's no laptop next to the machine, so the dashboard
needs to be viewable from a phone with nothing but the Pi itself providing
the network — no home WiFi, no internet required. The Pi hosts its own
hotspot and resolves a plain hostname to itself, so a phone gets a clean URL
with no IP address and no port.

**How it works:** the Pi's WiFi radio flips from *client* to *access point*
(`nmcli device wifi hotspot`), broadcasting its own network instead of
joining one; NetworkManager's hotspot mode already runs a small DNS server
(`dnsmasq`) for connected clients, and one custom line tells it "when anyone
asks for `timtam.box`, answer with the Pi's own address" — without that,
the phone could only reach the dashboard by typing the raw IP. Running the
dashboard on port 80 (the port browsers assume for a plain `http://` URL) is
the only reason no `:5000` is needed either. Put together: phone joins
`timtam` → asks the Pi's own DNS "where's `timtam.box`?" → Pi answers itself
→ browser connects on port 80 → dashboard loads.

**Current live values:**

| | |
|---|---|
| WiFi SSID | `timtam` |
| WiFi password | `maintain123` |
| Dashboard URL (once phone has joined) | `http://timtam.box` |
| Pi's hotspot IP | `10.42.0.1` |

**If `http://timtam.box` doesn't load on some device** (seen once: worked
immediately on a phone but not on a laptop, even after a hard refresh),
**go straight to `http://10.42.0.1` instead** — it's the Pi's fixed hotspot
IP, unaffected by whatever DNS setup that machine has (a manual DNS server,
a VPN, a resolver that doesn't pick up the hotspot's DHCP-assigned DNS,
etc.). The hostname depends on that device actually asking the Pi's own
DNS server; the raw IP skips DNS lookup entirely, so it always works as
long as the device has joined the `timtam` WiFi. Both devices just need to
be on that WiFi (not Ethernet) for either address to be reachable.

**One-time network setup** (already done on this Pi; shown here so it's
reproducible on a fresh one):

```bash
# 1. Pi broadcasts its own WiFi (needs a second network path, e.g. Ethernet,
#    for your own management access -- the hotspot takes over wlan0):
sudo nmcli device wifi hotspot ssid timtam password "maintain123"

# 2. Custom local DNS record so phones get a name instead of an IP
#    (NetworkManager's hotspot already runs its own dnsmasq instance):
echo "address=/timtam.box/10.42.0.1" | sudo tee /etc/NetworkManager/dnsmasq-shared.d/dashboard.conf
sudo nmcli connection down Hotspot && sudo nmcli connection up Hotspot

# 3. Make the hotspot start automatically on every boot (so this is a
#    one-time step, not something to remember to run before each demo):
sudo nmcli connection modify Hotspot connection.autoconnect yes
```

**Run the dashboard so the phone can reach it — default is the live CPX
data, not the simulated path.** Start/stop commands and pre-flight port/
serial diagnostics are in "Quick reference: start / stop the phone-facing
dashboard" at the very top of this README.

**Only if the live hardware chain fails on stage** — CPX unplugged, USB
port dead, no time to debug — fall back to the simulated J1939 path
instead, using its own flag name for the HTTP port (`--port`, not
`--http-port` — the two scripts name this differently):

```bash
sudo python3 dashboard.py --port 80
```

This is the recorded-backup path (see "Two demo paths" above): it replays a
committed synthetic dataset through the same detection core, not a live
sensor. Don't run it side-by-side with `cpx_dashboard.py` on the same
port — pick one.

Phone: join the `timtam` WiFi, browse to **http://timtam.box**. The
dashboard page also fires a phone-side alert (vibration + tone + screen
flash) the moment a fault is confirmed, so the phone doesn't need to be
actively watched to notice.

**Does the WiFi stay up when the dashboard server is stopped?** Yes — they
are fully independent. The hotspot is network infrastructure (a
NetworkManager connection); the dashboard is an application process.
Killing/restarting `cpx_dashboard.py` never touches the WiFi — a phone
stays joined to `timtam` throughout, it just gets connection-refused until
the dashboard process comes back. With `connection.autoconnect yes` (set
above), the hotspot also survives a full Pi reboot with no manual command
needed. The dashboard app itself is **not** auto-started on boot, and
deliberately so: unlike the WiFi, it depends on the CPX being plugged in,
warmed up, and healthy, and (see the known gap below) it has no
self-recovery if its serial connection hiccups — auto-starting it blindly
risks it silently dying with nobody noticing before a judge scans in. If
unattended "plug in power, walk away" reliability is ever needed, the right
fix is a `systemd` service with `Restart=on-failure` specifically for the
dashboard process, not just relying on autostart.

**For judges:** `qr/judge_access_card.html` is a self-contained display page
with two QR codes — scan `qr/wifi_qr.png` to join `timtam`, then
`qr/url_qr.png` to open the dashboard (a single code can't do both; a WiFi QR
and a URL QR are different payload formats). Regenerate either if the SSID,
password, or hostname ever changes:

```bash
qrencode -o qr/wifi_qr.png -s 10 -m 2 "WIFI:T:WPA;S:timtam;P:maintain123;;"
qrencode -o qr/url_qr.png -s 10 -m 2 "http://timtam.box"
```

**Fixed: serial auto-reconnect.** `cpx_dashboard.py` used to have no
recovery if the CPX's serial connection dropped even once — a transient
USB/CDC hiccup (confirmed via `dmesg`: the board never actually
re-enumerates, so it isn't a real unplug) would kill the pipeline thread
silently and freeze the dashboard on stale data forever. It now retries the
connection every 1.5 s until it succeeds again, with a visible
`reconnecting` status on the page in the meantime — no manual restart
needed.

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

# CPX sensor: flash CircuitPython 10.x + firmware/cpx_sensor.py per firmware/README.md,
# then plug into the Pi over USB (auto-detected by Adafruit USB VID).

# ESP32-C6 servo (optional): wire + flash per firmware/ESP32C6_ACTUATOR.md
# (needs arduino-cli + the esp32 core + ESP32Servo; the servo needs its OWN 5V).
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

# 6. LIVE HARDWARE DEMO: the real CPX loop (run on the Pi's own desktop so the
#    browser tab opens). Keep the board still for the ~8s baseline, then shake /
#    warm / press Button A. --gpio drives the real GPIO17 LED on a confirmed fault.
python3 cpx_dashboard.py --gpio
python3 cpx_dashboard.py --mock --fault-at 10   # rehearse with no hardware

# 7. RECORDED-BACKUP DEMO: the simulated J1939 loop (fallback if hardware hiccups).
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
