# CPX firmware — install

`cpx_sensor.py` is the sensor-edge firmware for the Circuit Playground
Express (CPX). It streams accelerometer, temperature, and mic-loudness
readings over USB serial as clean CSV lines for `cpx_serial_reader.py`
(on the Pi) to parse.

## Install

1. Put the CPX in CircuitPython mode: plug it into USB, double-tap the
   reset button if it isn't already showing up as a `CIRCUITPY` drive.
   (If the board arrives running Arduino/MakeCode firmware it enumerates
   as CDC-serial only with **no** `CIRCUITPY` drive — double-tap reset to
   get the `CPLAYBOOT` bootloader drive, then drag on the CircuitPython
   `.uf2` from https://circuitpython.org/board/circuitplayground_express/.
   Verified target here: **CircuitPython 10.2.1**.)
2. Download the [Adafruit CircuitPython
   Bundle](https://circuitpython.org/libraries) matching the CPX's
   CircuitPython version (check `CIRCUITPY/boot_out.txt`) — e.g. the
   `10.x-mpy` bundle for CircuitPython 10.x.
3. From the bundle's `lib/` folder, copy these into `CIRCUITPY/lib/`:
   - `adafruit_circuitplayground/` (folder)
   - `adafruit_lis3dh.mpy`
   - `adafruit_bus_device/` (folder)
   - `adafruit_thermistor.mpy`
   - `neopixel.mpy`
4. Copy `cpx_sensor.py` to `CIRCUITPY/` and rename it `code.py`
   (CircuitPython auto-runs `code.py` on boot/reset).
5. Open a serial terminal (`screen /dev/tty.usbmodem* 115200` on macOS,
   or the Mu editor's serial console) to confirm you see the header line
   followed by ~10 CSV lines/sec. Ctrl+C / close the terminal before
   handing the port to `cpx_serial_reader.py` — only one process can
   hold a serial port at a time.

## On-stage demo notes

- **Induce a fault by shaking the board** — the accel columns spike.
  Mirrors unbalanced/worn rotating machinery vibration.
- **Induce a fault by warming the board** (cup it in your hand, or a
  hairdryer from a few inches away) — `temp_c` climbs. Mirrors
  overheating.
- **Button A** is a manual trigger flag (column 8, `button_a`), reported
  as-is alongside real sensor data — it is never blended into the accel/
  temp/sound values. It's the demo-safety fallback if a physical
  shake/warm doesn't read clearly on stage; NeoPixels turn red while
  held, dim green otherwise (alive/idle heartbeat).
- **Microphone on CircuitPython 10.x:** the CircuitPlayground library
  stubs out `cp.sound_level` as "unsupported on Express", so the firmware
  drives the PDM mic directly via `audiobusio` and computes a
  mean-subtracted RMS loudness. If mic setup ever fails it degrades to
  `sound_level=0` (with a one-time `# mic: unavailable` comment) so the
  accel/temp/button stream never stops. Verified live: sound_level tracks
  ambient noise (idle ~40, loud >1000).
- Sample rate is 10 Hz (`SAMPLE_HZ` in `cpx_sensor.py`) — comfortably
  inside the CPX's I2C/PDM read budget and plenty to see a shake
  transient across several consecutive rows.
