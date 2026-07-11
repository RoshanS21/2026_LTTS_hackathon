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

### macOS bring-up recipe (VERIFIED 2026-07-10, C6-DevKitC-1)

The whole toolchain was reproduced on a Mac and used to flash a spare C6. The
gotchas below cost real time, so they're written down:

```bash
brew install arduino-cli
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32          # large (~1 GB, RISC-V toolchain)
arduino-cli lib install ESP32Servo

# Flash over the C6's NATIVE USB port (the port labelled "USB", not "UART"):
arduino-cli compile --fqbn "esp32:esp32:esp32c6:CDCOnBoot=cdc" firmware/esp32c6_actuator
arduino-cli upload  --fqbn "esp32:esp32:esp32c6:CDCOnBoot=cdc" \
  -p /dev/cu.usbmodem1101 firmware/esp32c6_actuator
```

- **`CDCOnBoot=cdc` matters.** It maps the firmware's `Serial` to whichever USB
  interface you're connected through. Flash/talk over the **native USB** port →
  `CDCOnBoot=cdc` (`Serial` = native USB CDC, `/dev/cu.usbmodem*`, Espressif VID
  `0x303A`). Flash/talk over the **UART/CP2102N** port instead →
  `CDCOnBoot=default` (`Serial` = UART0, `/dev/cu.usbserial*`, SiLabs VID
  `0x10C4`). Mismatch it and the board flashes fine but goes silent.
- **"Red LED on but no serial port" = charge-only cable** (or the dock/hub).
  The DevKitC-1's CP2102N enumerates on its own the instant a *data* cable is in
  the UART port, independent of firmware — if nothing appears, it's the cable or
  the port, not the board. Plug **directly into the machine**, use a known-data
  cable. Confirmed on macOS with: `ioreg -p IOUSB -l -w 0 | grep -iE '303A|10C4'`.
- **The native `USB` port can be flaky** where the `UART` port is fine (or vice
  versa) — one dead C6 in this project would present on neither, a spare
  presented on native USB only. If one port won't enumerate, try the other.
- **UART-port flashing needs manual download mode** if auto-reset fails
  (esptool: "No serial data received"): hold **BOOT**, tap **RST**, release
  BOOT, then upload. Native-USB flashing resets itself and doesn't need this.

## Troubleshooting: servo powered → board hangs / USB goes silent

Symptom: the C6 answers `PING`→`PONG` perfectly with the servo **unpowered**,
but the moment the servo has power the USB serial stops responding (the port
stays enumerated — `/dev/cu.usbmodem*` present — but no bytes flow). A power
cycle is required to recover; just unplugging the servo does **not** un-hang a
already-hung C6. Bisected on 2026-07-10 to: **healthy board, correct firmware,
correct pins (signal=GPIO2, GND=G), signal path fine — the trigger is purely
the _powered_ servo.**

Cause (bisected 2026-07-10): the hang was reproduced every time by **hot-plugging
the servo onto an already-running C6** — the connect-transient (inrush as the
servo's cap charges / the horn snaps to position, coupling through the shared
ground) crashes the C6's USB. Disconnecting the servo afterward does *not*
recover it; only a power cycle does. It is **not** a wrong-pin problem — moving
`SERVO_PIN` does not help — and it is **not** the signal or ground wire alone
(the board `PING`s fine with signal on GPIO2 + GND connected as long as the
servo is unpowered).

**Resolution (VERIFIED working, servo physically sweeping):** wire everything
first — signal→GPIO2, common ground, servo V+→external 5V — and *then* power the
C6 (fresh boot / power cycle). Booting with the whole rig already connected is
stable; hot-plugging the servo onto a live board is what breaks it.

Extra margin (reduces the transient, recommended but not strictly required once
the boot order is right):
1. **470–1000 µF electrolytic cap across the servo's V+/GND**, near the servo.
2. **Star-ground at the supply:** servo GND wire goes *directly* to the external
   supply's (−) terminal; a *separate* wire taps that same (−) to the C6 `G`.
   Never route the servo's return current *through* the C6 (`servo GND → C6 →
   supply` is the failure topology).
3. **Stiffer 5V supply** (≥1–2 A), short/thick wires, tight connections — loose
   breadboard jumpers add resistance and worsen any bounce.

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
