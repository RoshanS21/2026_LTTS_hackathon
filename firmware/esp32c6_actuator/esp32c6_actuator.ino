// esp32c6_actuator.ino
// =====================
// Piece 4 (live hardware path) of the LTTS edge-AI demo: the ESP32-C6 servo
// actuator ECU. It receives fault commands from the Pi over USB serial and
// physically actuates a servo -- the "act" step of perceive -> decide -> act,
// running on a SEPARATE microcontroller from the sensing (CPX) and the
// decision (Pi). That separation is the point: a real SDV actuates through a
// distinct ECU, not the gateway that made the call.
//
// Same demo-safe ethos as the rest of the repo: the Pi's decide() is the
// source of truth; this firmware only actuates. Unknown/garbled input is
// ignored (never hangs or crashes), and every command is acked over serial so
// the Pi can log the physical action into the dashboard's decision log.
//
// PROTOCOL (newline-terminated, case-insensitive), Pi -> C6:
//   ALARM   confirmed fault -> engage servo (continuous "flagging" sweep)
//   CLEAR   fault cleared   -> return servo to rest, stop sweeping
//   PING    liveness check
//   TEST    one self-test sweep, then rest
// C6 -> Pi acks: "READY" (on boot), "OK ALARM", "OK CLEAR", "PONG", "OK TEST".
//
// WIRING (see firmware/ESP32C6_ACTUATOR.md -- READ THE POWER NOTE):
//   Servo signal (orange/yellow) -> SERVO_PIN            (3.3V PWM is fine)
//   Servo V+     (red)           -> EXTERNAL 5V supply   (NOT the C6 or Pi!)
//   Servo GND    (brown/black)   -> external supply GND AND C6 GND  <-- common ground
//   The C6 itself is powered/programmed from the Pi's USB.
//   A servo drawing power through the C6/Pi will brown out the C6 on stall.
//
// BUILD (Arduino-ESP32 core + "ESP32Servo" library):
//   arduino-cli compile --fqbn esp32:esp32:esp32c6 firmware/esp32c6_actuator
//   arduino-cli upload  --fqbn esp32:esp32:esp32c6 -p /dev/ttyACM0 firmware/esp32c6_actuator

#include <ESP32Servo.h>

// --- configuration -------------------------------------------------------
static const int SERVO_PIN      = 2;    // any PWM-capable GPIO broken out on
                                        // your C6 board; change if 2 is taken
static const int REST_ANGLE     = 15;   // idle / "all clear" position (deg)
static const int SWEEP_ANGLE_A  = 60;   // alarm sweep endpoints
static const int SWEEP_ANGLE_B  = 120;
static const unsigned long SWEEP_MS = 350;  // half-period of the alarm sweep

// --- state ---------------------------------------------------------------
Servo servo;
bool alarming = false;
bool sweepToA = true;
unsigned long lastSweep = 0;
String cmd;

void setup() {
  Serial.begin(115200);
  servo.setPeriodHertz(50);              // standard 50 Hz hobby servo
  servo.attach(SERVO_PIN, 500, 2400);    // min/max pulse width (us)
  servo.write(REST_ANGLE);
  delay(300);
  Serial.println("# ESP32C6-ACT v1: cmds=ALARM|CLEAR|PING|TEST");
  Serial.println("READY");
}

void startAlarm() {
  alarming = true;
  sweepToA = true;
  lastSweep = millis();
  Serial.println("OK ALARM");
}

void stopAlarm() {
  alarming = false;
  servo.write(REST_ANGLE);
  Serial.println("OK CLEAR");
}

void handleCommand(String c) {
  c.trim();
  c.toUpperCase();
  if (c.length() == 0) return;
  if (c == "ALARM")      startAlarm();
  else if (c == "CLEAR") stopAlarm();
  else if (c == "PING")  Serial.println("PONG");
  else if (c == "TEST") {
    servo.write(SWEEP_ANGLE_B); delay(400);
    servo.write(REST_ANGLE);
    Serial.println("OK TEST");
  }
  // unknown command -> silently ignored, never crash the demo
}

void loop() {
  // Read commands without blocking the sweep.
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      handleCommand(cmd);
      cmd = "";
    } else if (cmd.length() < 40) {
      cmd += ch;
    }
  }

  // Non-blocking alarm sweep: the servo "wags" while a fault is active.
  if (alarming && (millis() - lastSweep >= SWEEP_MS)) {
    servo.write(sweepToA ? SWEEP_ANGLE_A : SWEEP_ANGLE_B);
    sweepToA = !sweepToA;
    lastSweep = millis();
  }
}
