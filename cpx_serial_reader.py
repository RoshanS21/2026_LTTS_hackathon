#!/usr/bin/env python3
"""
cpx_serial_reader.py
=====================
Piece 1b of the LTTS edge-AI demo: reads the CPX's live CSV-over-USB-serial
sensor stream (see firmware/cpx_sensor.py) and turns each line into a frame
dict the anomaly detector will consume next.

The wall-clock timestamp is assigned HERE, on the Pi, not on the CPX --
the CPX has no RTC and only knows milliseconds since its own boot
(device_ts_ms). This is the same role a real gateway plays against a
timestamp-less MCU sensor.

Frame dict keys: timestamp (ISO8601 UTC, Pi wall clock), asset_id,
accel_x_ms2, accel_y_ms2, accel_z_ms2, accel_mag_ms2 (computed here),
temp_c, sound_level, button_a, device_ts_ms.

Malformed/partial lines (boot banner noise, a write split across two
reads) are skipped with a warning on stderr -- never crash the demo.

Recorded-backup mindset: every live run is also appended to a CSV
(--out, default data/cpx_live_run.csv) as it streams, so a captured run
becomes an instant fallback dataset the same way data/scenarios/*.csv
already are for the simulated pipeline. That file is NOT committed --
unlike the synthetic scenarios, it would only be honest to commit an
actual hardware capture, so .gitignore excludes it (see repo .gitignore).

USAGE
-----
  python3 cpx_serial_reader.py                      # auto-detect CPX by USB VID, stream + log
  python3 cpx_serial_reader.py --port /dev/ttyACM0   # explicit port
  python3 cpx_serial_reader.py --duration 60         # capture for 60s then exit
  python3 cpx_serial_reader.py --mock                # no hardware -- synthetic frames, for
                                                       # developing/testing downstream pieces
  python3 cpx_serial_reader.py --mock --fault-at 5 --fault-duration 4
                                                       # inject a synthetic shake burst at t=5s

Requires pyserial for real hardware: `sudo apt install -y python3-serial`
(or `pip3 install pyserial`). Not required for --mock.
"""

import argparse
import csv
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

ADAFRUIT_VID = 0x239A
BAUD_RATE = 115200

FRAME_FIELDS = [
    "timestamp",
    "asset_id",
    "accel_x_ms2",
    "accel_y_ms2",
    "accel_z_ms2",
    "accel_mag_ms2",
    "temp_c",
    "sound_level",
    "button_a",
    "device_ts_ms",
]


def find_cpx_port():
    """Auto-detect the CPX's serial port by Adafruit's USB vendor ID."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for port in list_ports.comports():
        if port.vid == ADAFRUIT_VID:
            return port.device
    return None


def open_serial(port, timeout=1.0):
    import serial  # imported lazily -- not needed for --mock

    # write_timeout bounds cpx_dashboard's FAULT-command write: without it,
    # pyserial writes block forever by default, and since that write happens
    # on the same thread that reads sensor frames, a stuck/unplugged board
    # could freeze detection itself -- never allowed, per this repo's
    # never-depends-on-anything-else-succeeding rule for the detection core.
    return serial.Serial(port, BAUD_RATE, timeout=timeout, write_timeout=0.2)


def parse_line(line):
    """Parse one CSV line from the CPX into a frame dict, or None if invalid."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(",")
    if len(parts) != 8:
        return None
    try:
        device_ts_ms = int(parts[0])
        asset_id = parts[1]
        ax, ay, az = (float(parts[2]), float(parts[3]), float(parts[4]))
        temp_c = float(parts[5])
        sound_level = int(parts[6])
        button_a = int(parts[7])
    except ValueError:
        return None

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset_id": asset_id,
        "accel_x_ms2": ax,
        "accel_y_ms2": ay,
        "accel_z_ms2": az,
        "accel_mag_ms2": math.sqrt(ax * ax + ay * ay + az * az),
        "temp_c": temp_c,
        "sound_level": sound_level,
        "button_a": button_a,
        "device_ts_ms": device_ts_ms,
    }


def mock_lines(hz, fault_at, fault_duration):
    """Yield synthetic CPX-formatted CSV lines, matching the firmware's wire
    format exactly, for developing/testing without hardware attached."""
    period = 1.0 / hz
    start = time.monotonic()
    rng = random.Random(13)
    while True:
        loop_start = time.monotonic()
        elapsed_s = loop_start - start
        device_ts_ms = int(elapsed_s * 1000)

        in_fault = fault_at is not None and fault_at <= elapsed_s < (fault_at + fault_duration)

        # Fault shake must SUSTAIN |accel|-gravity above cpx_detector's
        # VIBRATION_DEV_MAX (8.0) on every frame, like a real on-stage shake
        # (live captures hit |accel| 20-60), or the dashboard's sustained-
        # shaking confirm gate never fires and the mock rehearsal shows
        # no event at all.
        ax = rng.uniform(-0.3, 0.3) + (rng.uniform(14, 24) if in_fault else 0.0)
        ay = rng.uniform(-0.3, 0.3) + (rng.uniform(-10, 10) if in_fault else 0.0)
        az = 9.8 + rng.uniform(-0.2, 0.2)
        temp_c = 24.0 + rng.uniform(-0.3, 0.3) + (elapsed_s * 0.01 if in_fault else 0.0)
        sound_level = int(90 + rng.uniform(-10, 10) + (rng.uniform(0, 60) if in_fault else 0))
        button_a = 0

        yield "{},{},{:.3f},{:.3f},{:.3f},{:.2f},{},{}".format(
            device_ts_ms, "CPX-EDGE-01-MOCK", ax, ay, az, temp_c, sound_level, button_a
        )

        remaining = period - (time.monotonic() - loop_start)
        if remaining > 0:
            time.sleep(remaining)


def stream(line_source, out_path, duration):
    os.makedirs(os.path.dirname(out_path), exist_ok=True) if os.path.dirname(out_path) else None
    write_header = not os.path.exists(out_path)

    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FRAME_FIELDS)
        if write_header:
            writer.writeheader()
            f.flush()

        start = time.monotonic()
        n_frames = 0
        n_skipped = 0
        try:
            for line in line_source:
                if duration is not None and (time.monotonic() - start) >= duration:
                    break
                frame = parse_line(line)
                if frame is None:
                    if line.strip() and not line.strip().startswith("#"):
                        n_skipped += 1
                        print(f"WARN: skipped malformed line: {line.strip()!r}", file=sys.stderr)
                    continue
                writer.writerow(frame)
                f.flush()
                n_frames += 1
                print(
                    f"[{frame['timestamp']}] asset={frame['asset_id']} "
                    f"accel=({frame['accel_x_ms2']:+.2f},{frame['accel_y_ms2']:+.2f},"
                    f"{frame['accel_z_ms2']:+.2f}) |a|={frame['accel_mag_ms2']:.2f} "
                    f"temp={frame['temp_c']:.1f}C sound={frame['sound_level']} "
                    f"button_a={frame['button_a']}"
                )
        except KeyboardInterrupt:
            pass

    print(f"\n{n_frames} frames written to {out_path} ({n_skipped} lines skipped)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default=None, help="Serial port (default: auto-detect by USB VID)")
    parser.add_argument("--out", default="data/cpx_live_run.csv", help="CSV file to append captured frames to")
    parser.add_argument("--duration", type=float, default=None, help="Stop after N seconds (default: run until Ctrl+C)")
    parser.add_argument("--mock", action="store_true", help="No hardware -- generate synthetic frames instead")
    parser.add_argument("--mock-hz", type=float, default=10.0, help="Mock sample rate (default: 10)")
    parser.add_argument("--fault-at", type=float, default=None, help="Mock only: inject a synthetic shake burst starting at t=N seconds")
    parser.add_argument("--fault-duration", type=float, default=3.0, help="Mock only: shake burst duration in seconds")
    args = parser.parse_args()

    if args.mock:
        print("Running in --mock mode (no hardware). Ctrl+C to stop.", file=sys.stderr)
        line_source = mock_lines(args.mock_hz, args.fault_at, args.fault_duration)
    else:
        port = args.port or find_cpx_port()
        if port is None:
            print(
                "ERROR: no CPX serial port found. Plug in the board, pass --port "
                "explicitly, or use --mock to test without hardware.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            ser = open_serial(port)
        except ImportError:
            print(
                "ERROR: pyserial is required for live hardware. Install with "
                "`sudo apt install -y python3-serial` (or `pip3 install pyserial`), "
                "or use --mock.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Connected to {port} @ {BAUD_RATE} baud. Ctrl+C to stop.", file=sys.stderr)
        line_source = (raw.decode("utf-8", errors="replace") for raw in ser)

    stream(line_source, args.out, args.duration)


if __name__ == "__main__":
    main()
