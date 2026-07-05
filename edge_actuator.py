#!/usr/bin/env python3
"""
edge_actuator.py
=================
Piece 6 of the LTTS edge-AI demo: the "act" step of a perceive -> decide ->
act loop, closing the Agentic/Physical AI gap. The detector perceives, the
policy here decides, and the Pi physically actuates a GPIO alarm (LED /
buzzer / relay on ALARM_PIN) -- no human and no network in the loop.

Design mirrors the rest of the pipeline's recorded-backup mindset:
  - decide() is pure and deterministic -- same policy whether or not
    hardware is present, so the decision log on the dashboard is always
    real even if the LED isn't wired.
  - The GPIO backend degrades gracefully to a mock that only logs, so the
    demo cannot crash because a wire fell out or gpiozero is missing.
  - Every decision and actuation is timestamped into an in-memory action
    log the dashboard renders, so judges see the loop even without
    watching the LED.

Policy:
  confirmed event (hard safety threshold breached)  -> ALARM  (GPIO on, fast blink)
  unconfirmed event (ML-only statistical flag)      -> MONITOR (log only -- don't
                                                       wake a driver for likely noise)
  event window closes                                -> CLEAR  (GPIO off)

USAGE
-----
  python3 edge_actuator.py --test            # 3 alarm pulses on the default pin
  python3 edge_actuator.py --test --pin 27   # different pin
  python3 edge_actuator.py --test --no-gpio  # exercise the mock backend
"""

import argparse
import threading
import time
from collections import deque
from datetime import datetime, timezone

ALARM_PIN = 17  # BCM numbering; LED/buzzer/relay module signal pin
MAX_LOG = 50


def decide(event):
    """Pure decision policy: event dict (piece 3 shape) -> action string."""
    return "alarm" if event.get("confirmed", True) else "monitor"


class Actuator:
    """Drives the alarm output and keeps a judge-visible action log.

    Tries real GPIO via gpiozero; if that fails for any reason (not a Pi,
    library missing, pin busy) it silently becomes a mock whose only output
    is the action log -- the decide() policy runs identically either way.
    """

    def __init__(self, pin=ALARM_PIN, use_gpio=True):
        self.pin = pin
        self._led = None
        self._lock = threading.Lock()
        self._log = deque(maxlen=MAX_LOG)
        self._alarm_active = False
        if use_gpio:
            try:
                from gpiozero import LED
                self._led = LED(pin)
            except Exception as e:  # noqa: BLE001
                self._record("INIT", f"GPIO unavailable ({e}); mock backend")
        if self._led is not None:
            self._record("INIT", f"GPIO{pin} armed (gpiozero)")
        self.backend = "gpio" if self._led is not None else "mock"

    def _record(self, action, detail):
        entry = {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "action": action,
            "detail": detail,
            "backend": "gpio" if self._led is not None else "mock",
        }
        with self._lock:
            self._log.append(entry)
        print(f"[actuator] {entry['action']}: {entry['detail']}", flush=True)
        return entry

    def handle_event(self, event, trigger="anomaly"):
        """Apply the policy to a newly-opened event. Returns the action taken."""
        action = decide(event)
        if action == "alarm":
            self.alarm_on(f"confirmed {trigger} on {event.get('asset_id', '?')}")
        else:
            self._record("MONITOR", f"ML-only flag on {event.get('asset_id', '?')}"
                                     " -- no hard breach, log only")
        return action

    def alarm_on(self, reason):
        with self._lock:
            already = self._alarm_active
            self._alarm_active = True
        if already:
            return
        if self._led is not None:
            self._led.blink(on_time=0.15, off_time=0.15)
        self._record("ALARM_ON", f"{reason} -> GPIO{self.pin} blinking")

    def alarm_off(self, reason="event window closed"):
        with self._lock:
            if not self._alarm_active:
                return
            self._alarm_active = False
        if self._led is not None:
            self._led.off()
        self._record("ALARM_OFF", f"{reason} -> GPIO{self.pin} off")

    @property
    def alarm_active(self):
        with self._lock:
            return self._alarm_active

    def log_entries(self):
        with self._lock:
            return list(self._log)

    def close(self):
        self.alarm_off("shutdown")
        if self._led is not None:
            self._led.close()


def main():
    ap = argparse.ArgumentParser(
        description="Self-test the GPIO alarm actuator (perceive->decide->act demo hardware)."
    )
    ap.add_argument("--pin", type=int, default=ALARM_PIN)
    ap.add_argument("--no-gpio", action="store_true",
                    help="Force the mock backend (exercise the policy/log path only)")
    ap.add_argument("--test", action="store_true", help="Run 3 alarm pulses and exit")
    args = ap.parse_args()

    if not args.test:
        print("Nothing to do; pass --test for a hardware self-test.")
        return

    act = Actuator(pin=args.pin, use_gpio=not args.no_gpio)
    print(f"Backend: {act.backend} (pin GPIO{args.pin})")

    fake_confirmed = {"confirmed": True, "asset_id": "SELF-TEST"}
    fake_soft = {"confirmed": False, "asset_id": "SELF-TEST"}
    for i in range(3):
        print(f"\npulse {i + 1}/3:")
        act.handle_event(fake_confirmed, trigger="self-test")
        time.sleep(1.2)
        act.alarm_off("self-test pulse done")
        time.sleep(0.4)

    print("\nunconfirmed event (should log MONITOR, no actuation):")
    act.handle_event(fake_soft)
    act.close()
    print("\nSelf-test complete. Action log:")
    for e in act.log_entries():
        print(f"  {e['ts']}  {e['action']:<10} {e['detail']}")


if __name__ == "__main__":
    main()
