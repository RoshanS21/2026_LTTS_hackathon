# Improvement ideas

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

## Remaining

### 1. Dry-run dashboard.py from the Pi's own screen before the real demo
Only ever validated over SSH with `--no-open`. The "browser tab auto-opens"
behavior has never been observed live — confirmed only that a desktop session
(labwc/Wayland) exists. Do one full dry run from the Pi's physical
keyboard/monitor before showtime. While there, confirm the LED on GPIO17
actually blinks during the two overheat events and one overspeed event
(the ALARM_ON log entries are already verified).

### 2. (Optional polish) Demo script for the pitch
60-second walkthrough: point at live sparklines -> first fault fires ->
LED blinks + card appears -> LLM diagnosis fills in -> end on the stats
tiles (5/5, 97.6% bandwidth saved). Practice once at --speed 30.
