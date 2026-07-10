# cpx_sensor.py -- copy this file to the CIRCUITPY drive as CODE.PY
# ====================================================================
# Piece 1a of the LTTS edge-AI demo: streams the Circuit Playground
# Express's onboard accelerometer, thermistor, and microphone over USB
# serial as clean CSV lines, so the Pi-side reader (cpx_serial_reader.py)
# can turn them into frames for the anomaly detector.
#
# This is the physical sensor edge: a fault is induced by physically
# shaking the board (accel spike -- mirrors unbalanced/worn rotating
# machinery) or warming it with a hand/hairdryer (temp rise -- mirrors
# overheating). The firmware does NOT decide what's anomalous -- it only
# reports raw signals, same separation of concerns as the rest of this
# pipeline (a generator/sensor emits, a detector decides).
#
# Button A is a transparent manual-trigger flag for demo safety (in case
# a physical shake doesn't land on stage) -- it is reported as-is
# alongside the real sensor values, never blended into them. Downstream
# code decides what to do with it. This mirrors the "recorded-backup
# mindset" used elsewhere in this repo (templated LLM fallback, mock
# GPIO backend): a fallback that's honest and visible, not one that
# fakes sensor data.
#
# Wire format (one line per sample, comma-separated, newline-terminated):
#   device_ts_ms,asset_id,accel_x_ms2,accel_y_ms2,accel_z_ms2,temp_c,sound_level,button_a
#
# device_ts_ms is milliseconds since the CPX booted (monotonic, NOT wall
# clock -- the CPX has no RTC). The Pi assigns the authoritative wall-clock
# timestamp when it receives each line.
#
# REQUIRED LIBRARIES (copy into CIRCUITPY/lib/ from the Adafruit
# CircuitPython Bundle matching your CircuitPython version):
#   adafruit_circuitplayground/  (folder)
#   adafruit_lis3dh.mpy
#   adafruit_bus_device/         (folder)
#   adafruit_thermistor.mpy
#   neopixel.mpy
#
# Tested target: CircuitPlayground Express, CircuitPython 8.x.

import time
import board
from adafruit_circuitplayground import cp

ASSET_ID = "CPX-EDGE-01"
SAMPLE_HZ = 10
PERIOD_S = 1.0 / SAMPLE_HZ

PROTOCOL_HEADER = (
    "# CPX-FRAME v1: device_ts_ms,asset_id,accel_x_ms2,accel_y_ms2,"
    "accel_z_ms2,temp_c,sound_level,button_a"
)

cp.pixels.brightness = 0.3
cp.pixels.fill((0, 0, 0))

print(PROTOCOL_HEADER)

start = time.monotonic()

while True:
    loop_start = time.monotonic()
    device_ts_ms = int((loop_start - start) * 1000)

    try:
        accel_x, accel_y, accel_z = cp.acceleration
    except TypeError:
        # accelerometer not ready yet on the very first loop(s); skip this sample
        accel_x = accel_y = accel_z = 0.0

    temp_c = cp.temperature
    sound_level = cp.sound_level
    button_a = 1 if cp.button_a else 0

    # heartbeat: dim green = alive/idle, red = manual fault trigger held
    cp.pixels.fill((255, 0, 0) if button_a else (0, 15, 0))

    print(
        "{},{},{:.3f},{:.3f},{:.3f},{:.2f},{},{}".format(
            device_ts_ms,
            ASSET_ID,
            accel_x,
            accel_y,
            accel_z,
            temp_c,
            sound_level,
            button_a,
        )
    )

    elapsed = time.monotonic() - loop_start
    remaining = PERIOD_S - elapsed
    if remaining > 0:
        time.sleep(remaining)
