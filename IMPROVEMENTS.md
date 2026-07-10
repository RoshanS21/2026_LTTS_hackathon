# Improvement ideas

## Done (2026-07-10)

- **Pivoted from the all-software J1939 pipeline to real hardware.** Decided
  with Roshan: keep the existing simulated pipeline as the recorded-backup
  demo path (README now says so explicitly), build a live 3-device chain
  (CPX sensor edge -> Pi detection+LLM+dashboard -> ESP32-C6 servo actuator)
  as the on-stage story.
- Piece 1 built: `firmware/cpx_sensor.py` (CPX CircuitPython firmware --
  accel/temp/mic-loudness over USB serial at 10Hz, button A as an honest
  manual-trigger flag, never blended into sensor values) +
  `cpx_serial_reader.py` (Pi-side reader: USB-VID auto-detect, malformed-line
  skip-with-warning, `--mock` mode for hardware-free testing). Self-tested
  via `--mock --fault-at` -- parser, CSV writer, and synthetic shake-burst
  injection all verified working without physical hardware attached.
- Not yet tested on the real CPX/Pi (no hardware in this environment) --
  first thing to verify once hardware is available: `firmware/README.md`'s
  install steps, then `python3 cpx_serial_reader.py` against the real board.

## Done (2026-07-05)

1. ~~Close the Agentic AI gap with real physical actuation~~ — `edge_actuator.py`:
   pure decide() policy + GPIO17 alarm via gpiozero (verified on this Pi with
   `--test`), mock fallback, judge-visible action log on the dashboard.
   Ramping faults escalate MONITOR -> ALARM live; hard safety breaches skip
   the debounce.
2. ~~Surface the real numbers where a judge can see them~~ — dashboard "Run
   stats" tiles + README "Measured results" table: 5/5 fault windows / 0
   false alarms / 19 blips debounced, ~5.8s median LLM latency, live
   bandwidth ledger (~97.6% uplink reduction, measured not asserted).
3. ~~Make detection feel like real streaming~~ — dashboard.py now replays the
   CSV row-by-row through live per-row detection (threshold + IsolationForest
   predict, ~3.4ms/row), live sparklines with threshold lines, events fire
   mid-stream, LLM summaries fill in asynchronously.

Also done along the way:
- Debounce (<3s runs dropped) + event-level metrics in anomaly_detector.py.
- Demo dataset seed 42 -> 13: all three fault types instead of 5x oil pressure.
- LLM prompt now names the flagged signal(s) (fixes overspeed event getting an
  overheat diagnosis) and only mentions the oil baseline when oil is flagged.
- warm_up() uses a full-size prompt (first live summary no longer risks the
  10s timeout).
- data/summaries.json snapshot regenerated with real LLM output (was all
  fallback templates).

## Done (2026-07-05, session 2)

4. ~~Scenario variety~~ — j1939_generator.py now has 4 scenario presets
   (mixed / healthy / degradation / stress), `--all` regenerates every CSV.
   New anomaly kinds: slow oil_decline drift + benign 1-2s sensor glitches
   (labeled NOT anomalous, stay inside hard bounds by design).
5. CUSUM drift detector added as a third tier (k=2.0, h=8.0, robust
   median/MAD baseline) — catches the slow decline 193s before the hard
   threshold. All hysteresis (merge_gap=3, evidence floors 3 confirmed /
   8 unconfirmed flagged rows) validated by a sweep across all scenarios.
6. Predictive lead time ("drift flagged Ns before safety breach") computed
   in both batch and streaming paths and shown on event cards.
7. False alarms (confirmed, GPIO fires) now counted separately from soft
   flags (monitor-only). Healthy scenario tile reads "clean run".
8. LLM timeout 10s -> 15s (a 12s real diagnosis beats an instant template;
   fallback still covers a truly stuck LLM).

Live dry run from the Pi's own screen was done by Roshan (browser tab
auto-opened, feed ran) — before the next GPIO demo, kill any old
`python3 dashboard.py` still running, it holds GPIO17 and forces the mock
backend ('GPIO busy').

## Remaining

### Hardware pipeline (current priority)
2. Adapt `anomaly_detector.py` (or a new sibling module) to consume live
   CPX frames (accel_x/y/z, accel_mag, temp_c, sound_level) instead of
   J1939 CSV rows. Threshold tier is straightforward; CUSUM drift tier
   should transfer almost directly (still a baseline + accumulator per
   signal); IsolationForest needs refit on CPX-shaped feature vectors.
3. Reuse `llm_summary.py`'s prompt-building + fallback-template pattern
   against the new event shape.
4. ESP32-C6 servo firmware, + optional Wiegand reader (5V, needs the
   BSS138 level shifter — see project brief for wiring notes).
5. Adapt `dashboard.py` to read from the live `cpx_serial_reader.py`
   stream instead of CSV replay.
6. First real-hardware validation pass (nothing above has touched actual
   hardware yet): flash the CPX, confirm `cpx_serial_reader.py` against
   the real board's serial port, physically induce a shake and a warm
   fault and confirm both show up as clean frames.

### Demo script for the pitch (optional polish)
Suggested 90-second arc, all verified working:
1. `python3 dashboard.py --csv data/scenarios/healthy.csv` (10s at high
   speed): "on a healthy machine it stays silent — 90 glitches debounced,
   zero alarms, zero bytes uplinked."
2. `python3 dashboard.py --csv data/scenarios/degradation.csv`: the crown
   jewel — MONITOR card opens on drift, LED ALARM fires when the bound
   breaks, card says "drift flagged 193s before safety breach."
3. End on mixed (default) stats tiles: 5/5 faults, 0 false alarms, ~97%
   bandwidth saved, ~6-12s on-Pi LLM diagnosis.
