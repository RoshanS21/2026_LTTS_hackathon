# Testing CPX live data on the Pi

Transport note for the SSH-to-Pi session. CPX is plugged into the Pi over
USB. Goal: confirm `firmware/cpx_sensor.py` + `cpx_serial_reader.py` produce
clean live frames from the real board.

## 0. One-time Pi prerequisites

```bash
sudo apt install -y python3-serial screen        # pyserial + a raw serial viewer
sudo usermod -aG dialout "$USER"                  # serial port access without sudo
# log out / back in (or `newgrp dialout`) for the group change to take effect
```

Without the `dialout` group you'll get "Permission denied: /dev/ttyACM0".
`newgrp dialout` applies it to the current shell without a full re-login.

## 1. Flash the firmware onto the CPX

The CPX shows up as a USB drive named `CIRCUITPY`. On a Pi desktop session it
auto-mounts at `/media/$USER/CIRCUITPY`. Over pure SSH it may not auto-mount —
easiest is to do the copy from the Pi's desktop file manager, or `lsblk` +
`mount` it manually.

- Copy `firmware/cpx_sensor.py` to `CIRCUITPY/code.py` (rename to `code.py`).
- Copy the 5 libraries into `CIRCUITPY/lib/` (see `firmware/README.md` for the
  exact list and which Adafruit bundle to pull).

CircuitPython auto-runs `code.py` on save/reset.

## 2. Find the port + raw sanity check

```bash
ls /dev/ttyACM*                    # CPX CDC serial, usually /dev/ttyACM0
screen /dev/ttyACM0 115200         # expect the "# CPX-FRAME v1: ..." header + ~10 CSV lines/sec
                                   # quit screen: Ctrl-A then K then Y
```

**Close `screen` before step 3** — only one process can hold the serial port.
Same goes for Mu/Thonny if either is open (they grab the port).

## 3. Run our reader against the live board

```bash
cd ~/workspace/2026_LTTS_hackathon      # or wherever you pulled it
python3 cpx_serial_reader.py --duration 20
```

Auto-detects the port by Adafruit's USB vendor ID (0x239A). Expect ~200 frames
in 20s, each line showing accel (x/y/z), |a|, temp, sound, button_a.

## 4. Physically induce faults while it runs

- **Shake the board** → accel magnitude `|a|` jumps well above the ~9.8 resting
  value (gravity). This is the vibration/worn-bearing fault.
- **Warm it** (cup in hand / brief hairdryer) → `temp` climbs. Overheat fault.
- **Press Button A** → `button_a=1`, NeoPixels go red. Manual demo-safety
  trigger; the value is reported as-is, never mixed into the sensor readings.

Confirm each change shows up in the printed frames in real time.

## 5. Verify the capture

```bash
tail -5 data/cpx_live_run.csv      # every frame is appended here as it streams
wc -l data/cpx_live_run.csv
```

`data/cpx_live_run.csv` is gitignored on purpose — only an actual hardware run
should ever live there, not a committed stand-in. It's created automatically
on first run.

## If something's off

- **No port / auto-detect fails:** `python3 cpx_serial_reader.py --port /dev/ttyACM0`
  to skip auto-detect. If `/dev/ttyACM0` doesn't exist, the firmware didn't
  boot — re-check `code.py` and `lib/` on the CIRCUITPY drive.
- **Permission denied:** `dialout` group not applied yet (see step 0), or run
  once with `sudo` just to confirm wiring (not for the real demo).
- **Garbled / partial first lines:** normal boot-banner noise; the reader skips
  malformed lines with a stderr warning and keeps going. Frame count in the
  final summary should still climb steadily.
- **Want to rehearse the flow without the board:** `python3 cpx_serial_reader.py
  --mock --fault-at 5 --fault-duration 4` (already verified working).
