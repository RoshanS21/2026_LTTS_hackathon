#!/usr/bin/env python3
"""
esp32_actuator.py
=================
Piece 4b (Pi side) of the LTTS edge-AI demo: sends the perceive -> decide ->
act command to the ESP32-C6 servo actuator ECU (firmware/esp32c6_actuator) over
USB serial. The Pi decides; the C6 actuates.

Reuses decide() from edge_actuator.py, so the GPIO-alarm path (simulated demo)
and this servo path (live hardware) share ONE policy and can't drift. The
ServoActuator interface mirrors edge_actuator.Actuator (handle_event /
alarm_on / alarm_off / alarm_active / log_entries / close / backend), so the
dashboard's "act" step is a drop-in swap between the two.

Recorded-backup ethos: if the ESP32-C6 isn't attached (no pyserial, no port,
open fails, or a mid-run write error), this degrades to a log-only mock. The
decide() policy and the dashboard's action log stay real even with no servo
wired -- the physical actuation is the only thing lost, never the decision.

USAGE
-----
  python3 esp32_actuator.py --test               # ALARM/CLEAR sweeps on the servo
  python3 esp32_actuator.py --test --no-serial   # mock backend (policy/log only)
  python3 esp32_actuator.py --test --port /dev/ttyACM1
"""

import argparse
import threading
import time
from collections import deque
from datetime import datetime, timezone

from edge_actuator import decide  # shared perceive->decide->act policy

BAUD = 115200
# USB-serial VIDs the ESP32-C6 commonly enumerates as: native USB-Serial-JTAG
# is Espressif 0x303A; boards with a bridge chip use CP210x 0x10C4 or CH340
# 0x1A86. Exclude the CPX's Adafruit VID so we never grab the sensor board.
ESP32_VIDS = {0x303A, 0x10C4, 0x1A86}
CPX_VID = 0x239A
MAX_LOG = 50


def find_esp32_port():
    """Auto-detect the ESP32-C6 serial port by USB vendor ID, skipping the CPX."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        if p.vid is not None and p.vid in ESP32_VIDS and p.vid != CPX_VID:
            return p.device
    return None


class ServoActuator:
    """Serial client for the ESP32-C6 servo ECU, degrading to a log-only mock
    if the board isn't reachable. Same interface as edge_actuator.Actuator."""

    def __init__(self, port=None, use_serial=True):
        self._ser = None
        self.port = None
        self._lock = threading.Lock()
        self._log = deque(maxlen=MAX_LOG)
        self._alarm_active = False
        if use_serial:
            self._connect(port)
        self.backend = "serial" if self._ser is not None else "mock"
        if self._ser is not None:
            self._record("INIT", f"ESP32-C6 servo on {self.port} (serial)")
        else:
            self._record("INIT", "ESP32-C6 not attached; mock backend (log only)")

    def _connect(self, port):
        try:
            import serial
        except ImportError:
            return
        port = port or find_esp32_port()
        if not port:
            return
        try:
            self._ser = serial.Serial(port, BAUD, timeout=0.5)
            self.port = port
            # The C6 resets when the port opens; wait for boot + READY banner.
            time.sleep(2.0)
            self._drain()
        except Exception:  # noqa: BLE001 -- any open failure -> mock
            self._ser = None

    def _drain(self):
        """Log any pending lines the C6 sent back (acks, boot banner)."""
        if self._ser is None:
            return
        try:
            while self._ser.in_waiting:
                line = self._ser.readline().decode("utf-8", "replace").strip()
                if line:
                    self._record("C6", line)
        except Exception:  # noqa: BLE001
            pass

    def _send(self, command):
        if self._ser is None:
            return
        try:
            self._ser.write((command + "\n").encode("utf-8"))
            self._ser.flush()
            time.sleep(0.05)
            self._drain()
        except Exception as e:  # noqa: BLE001 -- lost the board mid-run -> mock
            self._record("ERR", f"serial write failed ({e}); dropping to mock")
            self._ser = None
            self.backend = "mock"

    def _record(self, action, detail):
        entry = {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "action": action,
            "detail": detail,
            "backend": "serial" if self._ser is not None else "mock",
        }
        with self._lock:
            self._log.append(entry)
        print(f"[servo] {action}: {detail}", flush=True)
        return entry

    def handle_event(self, event, trigger="anomaly"):
        """Apply the shared policy to a newly-opened event."""
        action = decide(event)
        if action == "alarm":
            self.alarm_on(f"confirmed {trigger} on {event.get('asset_id', '?')}")
        else:
            self._record("MONITOR", f"statistical flag on {event.get('asset_id', '?')}"
                                    " -- no actuation")
        return action

    def alarm_on(self, reason):
        with self._lock:
            already = self._alarm_active
            self._alarm_active = True
        if already:
            return
        self._send("ALARM")
        self._record("ALARM_ON", f"{reason} -> servo engaged (sweep)")

    def alarm_off(self, reason="event window closed"):
        with self._lock:
            if not self._alarm_active:
                return
            self._alarm_active = False
        self._send("CLEAR")
        self._record("ALARM_OFF", f"{reason} -> servo rest")

    @property
    def alarm_active(self):
        with self._lock:
            return self._alarm_active

    def ping(self):
        """Liveness check; the C6 answers PONG (logged via _drain)."""
        self._send("PING")

    def log_entries(self):
        with self._lock:
            return list(self._log)

    def close(self):
        self.alarm_off("shutdown")
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass


def main():
    ap = argparse.ArgumentParser(
        description="ESP32-C6 servo actuator client (perceive->decide->act over serial)."
    )
    ap.add_argument("--port", default=None,
                    help="Serial port (default: auto-detect ESP32-C6 by USB VID)")
    ap.add_argument("--no-serial", action="store_true",
                    help="Force the mock backend (policy/log path only, no board)")
    ap.add_argument("--test", action="store_true",
                    help="Run 3 ALARM/CLEAR servo pulses and exit")
    args = ap.parse_args()

    if not args.test:
        print("Nothing to do; pass --test for a servo self-test.")
        return

    act = ServoActuator(port=args.port, use_serial=not args.no_serial)
    print(f"Backend: {act.backend}" + (f" ({act.port})" if act.port else ""))

    confirmed = {"confirmed": True, "asset_id": "SELF-TEST"}
    soft = {"confirmed": False, "asset_id": "SELF-TEST"}
    for i in range(3):
        print(f"\npulse {i + 1}/3:")
        act.handle_event(confirmed, trigger="self-test")
        time.sleep(1.5)   # let the servo sweep visibly
        act.alarm_off("self-test pulse done")
        time.sleep(0.8)

    print("\nunconfirmed event (should log MONITOR, no servo):")
    act.handle_event(soft)
    act.close()
    print("\nSelf-test complete. Action log:")
    for e in act.log_entries():
        print(f"  {e['ts']}  {e['action']:<10} {e['detail']}")


if __name__ == "__main__":
    main()
