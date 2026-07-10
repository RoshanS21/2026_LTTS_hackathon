# CPX firmware — install

`cpx_sensor.py` is the sensor-edge firmware for the Circuit Playground
Express (CPX). It streams accelerometer, temperature, and mic-loudness
readings over USB serial as clean CSV lines for `cpx_serial_reader.py`
(on the Pi) to parse.

## Install

1. Put the CPX in CircuitPython mode: plug it into USB, double-tap the
   reset button if it isn't already showing up as a `CIRCUITPY` drive.
2. Download the [Adafruit CircuitPython
   Bundle](https://circuitpython.org/libraries) matching the CPX's
   CircuitPython version (check `CIRCUITPY/boot_out.txt`).
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
- Sample rate is 10 Hz (`SAMPLE_HZ` in `cpx_sensor.py`) — comfortably
  inside the CPX's I2C/PDM read budget and plenty to see a shake
  transient across several consecutive rows.
