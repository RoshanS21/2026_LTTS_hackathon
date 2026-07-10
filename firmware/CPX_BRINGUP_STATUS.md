# CPX bring-up status (WIP handoff)

Live progress log for the CPX hardware bring-up, so any session can resume
instantly after a hiccup. Delete once Piece 1 is fully signed off.

_Last updated: 2026-07-10, during live hardware bring-up on the Pi._

## Milestone reached: CPX flashed and streaming

The CPX arrived running leftover **Arduino-style firmware** (enumerated
`239a:8018`, CDC-only, NO mass-storage / CIRCUITPY, streamed nothing). We
reflashed it over SSH and it now runs our sensor firmware.

**Done:**
- Board flashed with **CircuitPython 10.2.1** (`circuitplayground_express`,
  samd21g18). `boot_out.txt` confirms.
- `firmware/cpx_sensor.py` copied to `CIRCUITPY/code.py`; 5 libs in
  `CIRCUITPY/lib/` (adafruit_circuitplayground/, adafruit_bus_device/,
  adafruit_lis3dh.mpy, adafruit_thermistor.mpy, neopixel.mpy).
- Board streams the 8-column frame at ~10 Hz on `/dev/ttyACM0`.
- `cpx_serial_reader.py` verified live: 298 frames @ 9.9 Hz, 1 boot-banner
  line skipped gracefully, Pi wall-clock timestamps assigned, appended to
  `data/cpx_live_run.csv` (gitignored — this is now a REAL capture).

**Signal verification — ALL FOUR confirmed. Piece 1 SIGNED OFF.**
Clean 198-frame capture (2026-07-10 17:55Z, `data/cpx_live_run.csv`):
- shake: `|accel|` 7.6→**21.3** (peak −5.98, 8.81, 18.50) ✅
- warm:  22.0→**28.2 °C** (6.2 °C swing) ✅
- Button A: **24 frames** held (~2.4 s), NeoPixels red ✅
- mic: **36→100** ✅
198 frames @ 9.9 Hz, 1 boot-banner line skipped gracefully, Pi timestamps
assigned. The full board→serial→reader→CSV path is proven end to end.

## Mic gotcha (already fixed — don't re-break it)

On CircuitPython 10.x for the CPX, `cp.sound_level` raises
`NotImplementedError` ("not supported on Circuit Playground Express") — the
library stubs it out. `audiobusio` IS in the build, so the firmware now
drives the PDM mic directly (`board.MICROPHONE_CLOCK/DATA`) and computes a
mean-subtracted RMS, discarding the first (settling) capture. If mic setup
ever fails it degrades to `sound_level=0` + a one-time `# mic: unavailable`
comment — accel/temp/button keep streaming. This is why the firmware header
now says "tested 10.2.1".

## How to re-flash from scratch (if the board gets wiped/reset)

Staging lives in the **ephemeral** session scratchpad
(`/tmp/claude-.../scratchpad/`) — re-download if starting fresh:

```bash
# 1. UF2 (CircuitPython 10.2.1 for CPX)
curl -sL -o cpx_cp.uf2 \
 https://downloads.circuitpython.org/bin/circuitplayground_express/en_US/adafruit-circuitpython-circuitplayground_express-en_US-10.2.1.uf2
# 2. Library bundle (10.x) -> extract the 5 libs listed above
curl -sL -o bundle.zip \
 https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases/download/20260710/adafruit-circuitpython-bundle-10.x-mpy-20260710.zip

# 3. PHYSICAL: double-tap CPX center reset -> green LEDs -> CPLAYBOOT drive
#    (single tap = red flash = normal reboot, won't work; must be a fast double-tap)
# 4. Copy UF2 onto CPLAYBOOT (auto-reboots into CircuitPython):
udisksctl mount -b /dev/sda ; cp cpx_cp.uf2 /media/$USER/CPLAYBOOT/ ; sync
# 5. After CIRCUITPY appears, copy firmware + libs:
cp firmware/cpx_sensor.py /media/$USER/CIRCUITPY/code.py
cp -r <staged lib>/. /media/$USER/CIRCUITPY/lib/ ; sync
```

CircuitPython auto-reloads code.py on write. To force a reload over serial:
open `/dev/ttyACM0` @115200, send Ctrl-C then Ctrl-D.

## Env gotchas
- Only one process can hold `/dev/ttyACM0` — close screen/Mu/Thonny and any
  stray python before running the reader.
- CPX bootloader = **double-tap** center reset (green, stays). Single tap
  (red flash) just reboots the app firmware.
- `data/cpx_live_run.csv` is gitignored; keep it for real captures only.

## Next task (Task 2, per the plan)
Adapt `anomaly_detector.py` to consume CPX frames (accel_mag / temp_c /
sound_level) instead of J1939 SPN columns — reuse the 3-tier detector core;
add a CPX signal profile (columns, bounds, fault semantics, prompt) the same
way the J1939 path works. Then Task 3 (llm_summary prompt/fallback for the
new event shape), Task 4 (ESP32-C6 servo), Task 5 (dashboard on live serial).
