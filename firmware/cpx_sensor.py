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
# MICROPHONE NOTE (CircuitPython 10.x on the Circuit Playground Express):
# the CircuitPlayground library's `cp.sound_level` is stubbed out as
# "unsupported on Express" in current library builds, so we read the PDM
# mic directly via audiobusio (which IS in the CPX build) and compute a
# mean-subtracted RMS loudness ourselves. If mic setup fails for any
# reason, we degrade gracefully: sound_level is reported as 0 and a
# one-time "# mic: unavailable" comment is emitted -- the accel/temp/
# button signals (the demo-critical ones) keep streaming regardless.
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
# Tested target: CircuitPlayground Express, CircuitPython 10.2.1
# (also works on 8.x; the mic fallback above is what makes 10.x fine).

import math
import time

import board
from adafruit_circuitplayground import cp

ASSET_ID = "CPX-EDGE-01"
SAMPLE_HZ = 10
PERIOD_S = 1.0 / SAMPLE_HZ

MIC_SAMPLES = 160  # ~10 ms at 16 kHz; enough for a loudness estimate, cheap on RAM

PROTOCOL_HEADER = (
    "# CPX-FRAME v1: device_ts_ms,asset_id,accel_x_ms2,accel_y_ms2,"
    "accel_z_ms2,temp_c,sound_level,button_a"
)


def setup_mic():
    """Return (record_fn, sample_buffer) for the onboard PDM mic, or
    (None, None) if the mic can't be initialised. Kept optional on purpose:
    the mic is the least important of the four signals and must never stop
    the accel/temp/button stream."""
    try:
        import array

        import audiobusio

        mic = audiobusio.PDMIn(
            board.MICROPHONE_CLOCK,
            board.MICROPHONE_DATA,
            sample_rate=16000,
            bit_depth=16,
        )
        buf = array.array("H", [0] * MIC_SAMPLES)
        mic.record(buf, len(buf))  # discard the first (settling) capture
        return mic, buf
    except Exception:  # noqa: BLE001 -- any failure -> graceful no-mic mode
        return None, None


def sound_level(mic, buf):
    """Mean-subtracted RMS of a fresh mic capture (AC loudness, DC removed).
    Returns 0 if the mic is unavailable."""
    if mic is None:
        return 0
    try:
        mic.record(buf, len(buf))
        mean = sum(buf) / len(buf)
        return int(math.sqrt(sum((s - mean) ** 2 for s in buf) / len(buf)))
    except Exception:  # noqa: BLE001
        return 0


cp.pixels.brightness = 0.3
cp.pixels.fill((0, 0, 0))

mic, mic_buf = setup_mic()

print(PROTOCOL_HEADER)
if mic is None:
    print("# mic: unavailable -- sound_level reported as 0 (accel/temp/button unaffected)")

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
    sound = sound_level(mic, mic_buf)
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
            sound,
            button_a,
        )
    )

    elapsed = time.monotonic() - loop_start
    remaining = PERIOD_S - elapsed
    if remaining > 0:
        time.sleep(remaining)
