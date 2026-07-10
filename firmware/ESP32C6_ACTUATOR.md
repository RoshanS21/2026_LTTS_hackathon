# ESP32-C6 servo actuator — wiring & flashing

Piece 4 of the live hardware path: the ESP32-C6 receives fault commands from
the Pi over USB serial (`esp32_actuator.py`) and drives a servo — the physical
"act" step, on a separate ECU from the CPX sensor and the Pi decision core.

Firmware: [`esp32c6_actuator/esp32c6_actuator.ino`](esp32c6_actuator/esp32c6_actuator.ino).

## Wiring — READ THE POWER NOTE

The Pi's 5V is healthy (measured `EXT5V = 5.13 V`, no throttling), and the
**ESP32-C6 logic** runs fine off the Pi's USB. But the **servo must have its
own 5V supply** — a servo pulls ~0.7 A (SG90) to ~2.5 A (MG996R) at
stall/startup, and those spikes will brown out the C6 if its power runs
through the board or the Pi. The onboard 3.3V regulator can't drive a servo
at all.

```
ESP32-C6 ── USB ──> Raspberry Pi        (logic power + serial commands)

Servo signal (orange/yellow) ─────────> ESP32-C6 SERVO_PIN (GPIO2 default)
Servo V+     (red)           ─────────> EXTERNAL 5V supply (+)
Servo GND    (brown/black)   ──┬──────> EXTERNAL 5V supply (−)
                               └──────> ESP32-C6 GND        <-- COMMON GROUND (mandatory)
```

- External 5V: a 5V/2A USB charger (breakout cable), a power bank, or a 4×AA
  pack (~6 V, in-spec for most servos). 5V/1A survives one SG90; 2A gives margin.
- Put a **470–1000 µF electrolytic cap across the servo's 5V/GND** near the
  servo to soak up the current spikes.
- 3.3 V PWM from the C6 drives SG90/MG90-class servos fine. The common ground
  is what makes the signal valid — without it the servo sees no reference.
- `SERVO_PIN` defaults to GPIO2; change it in the `.ino` if that pin is taken
  on your particular C6 board.

## Flashing (Arduino-ESP32 via arduino-cli)

`arduino-cli` is not yet installed on the Pi. One-time setup:

```bash
# 1. arduino-cli
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
export PATH="$PWD/bin:$PATH"          # or move ./bin/arduino-cli onto your PATH

# 2. ESP32 board package
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32

# 3. Servo library
arduino-cli lib install ESP32Servo
```

Then compile + upload (board plugged into the Pi):

```bash
arduino-cli compile --fqbn esp32:esp32:esp32c6 firmware/esp32c6_actuator
arduino-cli upload  --fqbn esp32:esp32:esp32c6 -p /dev/ttyACM1 firmware/esp32c6_actuator
# ^ use the C6's port; with the CPX also plugged in it's likely ttyACM1.
#   `ls /dev/ttyACM*` before/after plugging the C6 to identify it.
```

## Serial protocol (Pi → C6, newline-terminated)

| Command | Effect | Ack |
|---|---|---|
| `ALARM` | engage servo, continuous "flagging" sweep | `OK ALARM` |
| `CLEAR` | return servo to rest, stop sweeping | `OK CLEAR` |
| `PING`  | liveness check | `PONG` |
| `TEST`  | one sweep then rest | `OK TEST` |

On boot the C6 prints `# ESP32C6-ACT v1: ...` then `READY`. Unknown input is
ignored (never crashes the demo).

## Test it from the Pi

```bash
python3 esp32_actuator.py --test                 # auto-detects the C6 by USB VID
python3 esp32_actuator.py --test --port /dev/ttyACM1
python3 esp32_actuator.py --test --no-serial      # mock backend, no board (verified)
```

The Pi client reuses `decide()` from `edge_actuator.py` (shared policy) and
degrades to a log-only mock if the board isn't attached — the decision log
stays real even with no servo wired. It mirrors `edge_actuator.Actuator`'s
interface, so the live dashboard (Task 5) can swap the GPIO alarm for the
servo without touching the perceive→decide→act wiring.
