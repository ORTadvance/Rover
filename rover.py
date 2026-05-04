#!/usr/bin/env python3
"""
rover.py — Onboard rover script for "Error 404: Gravity Not Found"
Team:        QMSEDS / Queen Mary University of London
Competition: Olympus Rover Trials 2025-2026

Runs on: Raspberry Pi 4B
Comms:   ZeroMQ TCP to middleman broker (middleman.py)

Responsibilities:
  - Receive motor / servo / stop commands from middleman
  - Drive 4 wheels via 2x MDD10A dual-channel motor drivers
  - Control camera pitch servo (MG92B)
  - Read front and rear digital IR obstacle sensors
  - Stream compressed video frames to middleman
  - Detect and auto-capture QR codes (OpenCV + pyzbar)
  - Publish telemetry (motor state, IR readings, QR results, heartbeat age)
  - Enter SAFE STATE (motors stop) if heartbeat from base station is lost

MOCK_MODE = True  → runs on any PC without GPIO hardware (for simulation).
MOCK_MODE = False → runs on the real Pi with pigpio daemon (sudo pigpiod).

ZeroMQ socket roles (rover CONNECTs, middleman BINDs):
  cmd_sub    SUB   ← middleman PUB  — receives drive/servo/stop/heartbeat commands
  telem_push PUSH  → middleman PULL — sends telemetry JSON at ~10 Hz
  video_push PUSH  → middleman PULL — sends JPEG frames at ~FRAME_RATE Hz
"""

import asyncio
import json
import logging
import os
import struct
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import zmq
import zmq.asyncio
from pyzbar import pyzbar

# ── Toggle this before deploying to real hardware ────────────────────────────
MOCK_MODE = True

if not MOCK_MODE:
    import pigpio  # only available on Raspberry Pi with pigpiod running


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  (update GPIO pins once electrical team confirms wiring)
# ══════════════════════════════════════════════════════════════════════════════

# MDD10A Driver 1 — left wheels (front CH1, rear CH2)
LEFT_FRONT_PWM = 12
LEFT_FRONT_DIR = 6
LEFT_REAR_PWM  = 13
LEFT_REAR_DIR  = 5

# MDD10A Driver 2 — right wheels (front CH1, rear CH2)
RIGHT_FRONT_PWM = 18
RIGHT_FRONT_DIR = 24
RIGHT_REAR_PWM  = 19
RIGHT_REAR_DIR  = 25

# IR sensors — digital, active-low (obstacle detected = GPIO reads LOW)
IR_FRONT_PIN = 23
IR_BACK_PIN  = 22

# Camera pitch servo (MG92B) — pulse widths in microseconds
SERVO_PIN    = 17
SERVO_MIN_PW = 500   # full down
SERVO_MID_PW = 1500  # centre / neutral
SERVO_MAX_PW = 2500  # full up

# Motor constraints
PWM_FREQUENCY = 1000  # Hz
MAX_SPEED     = 1.0   # normalised magnitude cap [0.0, 1.0]

# Middleman broker — update IP once network is configured
MIDDLEMAN_HOST    = "192.168.1.100"
CMD_SUB_PORT      = 5556   # rover SUBs here (middleman PUBs commands)
TELEMETRY_PORT    = 5557   # rover PUSHes here (middleman PULLs telemetry)
VIDEO_PORT        = 5558   # rover PUSHes here (middleman PULLs frames)

# Heartbeat
HEARTBEAT_TIMEOUT  = 2.0   # seconds — safe state triggered if exceeded
HEARTBEAT_INTERVAL = 0.5   # seconds — rover sends its own heartbeat this often

# Camera / vision
CAMERA_INDEX     = 0
FRAME_WIDTH      = 640
FRAME_HEIGHT     = 480
FRAME_RATE       = 30
QR_BLUR_MIN      = 100.0  # Laplacian variance threshold — blurry frames skipped
JPEG_QUALITY     = 80

# Mission logging directory (timestamped per session)
LOG_DIR = Path("mission_logs") / datetime.now().strftime("%Y%m%d_%H%M%S")


# ══════════════════════════════════════════════════════════════════════════════
# ENUMS AND DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

class RoverState(Enum):
    IDLE       = auto()  # not yet connected
    OPERATING  = auto()  # receiving valid heartbeats — normal operation
    SAFE_STATE = auto()  # heartbeat lost — motors stopped until restored
    ERROR      = auto()  # unrecoverable hardware fault


@dataclass
class Telemetry:
    """All data the rover publishes to the middleman at ~10 Hz."""
    timestamp:     str   = ""
    state:         str   = RoverState.IDLE.name
    left_speed:    float = 0.0   # normalised [-1, 1]
    right_speed:   float = 0.0
    servo_pw:      int   = SERVO_MID_PW
    ir_front:      bool  = False  # True = obstacle detected in front
    ir_back:       bool  = False  # True = obstacle detected at rear
    qr_detected:   bool  = False
    qr_data:       str   = ""
    qr_image_path: str   = ""
    heartbeat_age: float = 0.0   # seconds since last heartbeat from base station


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rover")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s")
    fh = logging.FileHandler(LOG_DIR / "rover.log")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = _setup_logging()


# ══════════════════════════════════════════════════════════════════════════════
# HARDWARE ABSTRACTION  (real pigpio calls or silent mock)
# ══════════════════════════════════════════════════════════════════════════════

class GPIO:
    """
    Wraps pigpio so the rest of the code never imports it directly.
    In MOCK_MODE every call is a no-op, allowing full simulation on any PC.
    """

    def __init__(self):
        if MOCK_MODE:
            log.info("[GPIO] MOCK MODE — no hardware access")
            self._pi = None
        else:
            self._pi = pigpio.pi()
            if not self._pi.connected:
                raise RuntimeError(
                    "Cannot connect to pigpio daemon. Run: sudo pigpiod"
                )
            log.info("[GPIO] Connected to pigpio daemon")

    def mode_output(self, pin: int):
        if self._pi:
            self._pi.set_mode(pin, pigpio.OUTPUT)

    def mode_input(self, pin: int):
        if self._pi:
            self._pi.set_mode(pin, pigpio.INPUT)

    def set_pwm_freq(self, pin: int, freq: int):
        if self._pi:
            self._pi.set_PWM_frequency(pin, freq)

    def set_pwm_duty(self, pin: int, duty: int):
        """duty 0-255"""
        if self._pi:
            self._pi.set_PWM_dutycycle(pin, duty)

    def write(self, pin: int, level: int):
        if self._pi:
            self._pi.write(pin, level)

    def read(self, pin: int) -> int:
        if self._pi:
            return self._pi.read(pin)
        return 1  # mock: active-low sensor → 1 means no obstacle

    def set_servo_pw(self, pin: int, pw: int):
        if self._pi:
            self._pi.set_servo_pulsewidth(pin, pw)

    def close(self):
        if self._pi:
            self._pi.stop()


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR CONTROLLER
# ══════════════════════════════════════════════════════════════════════════════

class MotorController:
    """
    Four-wheel differential drive via two MDD10A dual-channel drivers.

    Speed is normalised:  +1.0 = full forward,  -1.0 = full reverse,  0 = stop.

    Wiring assumed:
      Driver 1 CH1 → left front   |  Driver 1 CH2 → left rear
      Driver 2 CH1 → right front  |  Driver 2 CH2 → right rear
    """

    _LEFT_PINS  = [(LEFT_FRONT_PWM,  LEFT_FRONT_DIR),
                   (LEFT_REAR_PWM,   LEFT_REAR_DIR)]
    _RIGHT_PINS = [(RIGHT_FRONT_PWM, RIGHT_FRONT_DIR),
                   (RIGHT_REAR_PWM,  RIGHT_REAR_DIR)]

    def __init__(self, gpio: GPIO):
        self._gpio = gpio
        for pwm, dir_ in self._LEFT_PINS + self._RIGHT_PINS:
            gpio.mode_output(pwm)
            gpio.mode_output(dir_)
            gpio.set_pwm_freq(pwm, PWM_FREQUENCY)
        self._left  = 0.0
        self._right = 0.0
        log.info("[Motors] Initialised — 4WD via 2x MDD10A")

    def _apply(self, pins: list, speed: float):
        speed = max(-MAX_SPEED, min(MAX_SPEED, speed))
        forward = 1 if speed >= 0 else 0
        duty    = int(abs(speed) * 255)
        for pwm, dir_ in pins:
            self._gpio.write(dir_, forward)
            self._gpio.set_pwm_duty(pwm, duty)

    def set_speeds(self, left: float, right: float):
        self._apply(self._LEFT_PINS,  left)
        self._apply(self._RIGHT_PINS, right)
        self._left  = left
        self._right = right
        if MOCK_MODE:
            log.debug(f"[Motors] L={left:+.2f}  R={right:+.2f}")

    def stop(self):
        self.set_speeds(0.0, 0.0)

    @property
    def speeds(self) -> tuple[float, float]:
        return self._left, self._right


# ══════════════════════════════════════════════════════════════════════════════
# CAMERA SERVO
# ══════════════════════════════════════════════════════════════════════════════

class CameraServo:
    """Single-axis (pitch) servo control for the camera mount (MG92B)."""

    def __init__(self, gpio: GPIO):
        self._gpio = gpio
        self._pw   = SERVO_MID_PW
        gpio.set_servo_pw(SERVO_PIN, SERVO_MID_PW)
        log.info(f"[Servo] Initialised at centre ({SERVO_MID_PW} µs)")

    def set_pulsewidth(self, pw: int):
        pw = max(SERVO_MIN_PW, min(SERVO_MAX_PW, pw))
        self._pw = pw
        self._gpio.set_servo_pw(SERVO_PIN, pw)
        if MOCK_MODE:
            log.debug(f"[Servo] pw={pw} µs")

    @property
    def pulsewidth(self) -> int:
        return self._pw


# ══════════════════════════════════════════════════════════════════════════════
# IR SENSOR MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class IRSensorManager:
    """
    Reads front and rear digital IR obstacle sensors.
    Sensors are active-low: GPIO LOW = obstacle present.
    """

    def __init__(self, gpio: GPIO):
        self._gpio = gpio
        gpio.mode_input(IR_FRONT_PIN)
        gpio.mode_input(IR_BACK_PIN)
        log.info("[IR] Front and rear sensors initialised")

    def read(self) -> tuple[bool, bool]:
        """Returns (front_obstacle, rear_obstacle)."""
        front = not bool(self._gpio.read(IR_FRONT_PIN))
        back  = not bool(self._gpio.read(IR_BACK_PIN))
        return front, back


# ══════════════════════════════════════════════════════════════════════════════
# VISION PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

class VisionPipeline:
    """
    Captures frames from the Pi Camera Module 3 in a background thread.

    Each frame is:
      1. Scanned for QR codes with pyzbar.
      2. Auto-saved as JPEG if a QR is found AND the frame is sharp enough
         (Laplacian variance >= QR_BLUR_MIN).
      3. JPEG-compressed and stored for the video streaming loop.

    In MOCK_MODE a synthetic frame (black + timestamp text) is generated
    instead of accessing a real camera.
    """

    def __init__(self, save_dir: Path):
        self._save_dir = save_dir / "captures"
        self._save_dir.mkdir(parents=True, exist_ok=True)
        self._cap: Optional[cv2.VideoCapture] = None
        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._qr_result: dict = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        if MOCK_MODE:
            log.info("[Vision] MOCK MODE — synthetic frames")
            self._thread = threading.Thread(target=self._mock_loop, daemon=True)
        else:
            self._cap = cv2.VideoCapture(CAMERA_INDEX)
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            self._cap.set(cv2.CAP_PROP_FPS,          FRAME_RATE)
            if not self._cap.isOpened():
                raise RuntimeError("Camera could not be opened")
            log.info("[Vision] Camera opened")
            self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._cap:
            self._cap.release()

    # ── capture loops ────────────────────────────────────────────────────────

    def _mock_loop(self):
        while self._running:
            frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
            label = f"MOCK FRAME  {datetime.now().strftime('%H:%M:%S.%f')[:-3]}"
            cv2.putText(frame, label, (20, FRAME_HEIGHT // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 80), 2)
            self._process(frame)
            time.sleep(1.0 / FRAME_RATE)

    def _capture_loop(self):
        while self._running:
            ok, frame = self._cap.read()
            if not ok:
                log.warning("[Vision] Frame read failed — retrying")
                time.sleep(0.05)
                continue
            self._process(frame)

    # ── frame processing ─────────────────────────────────────────────────────

    def _process(self, frame: np.ndarray):
        annotated = frame.copy()
        qr_codes  = pyzbar.decode(frame)

        for qr in qr_codes:
            data = qr.data.decode("utf-8", errors="replace")
            pts  = np.array(qr.polygon, dtype=np.int32)
            cv2.polylines(annotated, [pts], True, (0, 255, 0), 2)
            cv2.putText(annotated, data, (pts[0][0], pts[0][1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

            blur = self._blur_score(frame)
            if blur >= QR_BLUR_MIN:
                ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                path = str(self._save_dir / f"qr_{ts}.jpg")
                cv2.imwrite(path, frame)
                with self._lock:
                    self._qr_result = {
                        "data":       data,
                        "image_path": path,
                        "timestamp":  ts,
                        "blur_score": round(blur, 1),
                    }
                log.info(f"[Vision] QR captured — '{data}' → {path}  blur={blur:.1f}")

        _, jpeg = cv2.imencode(
            ".jpg", annotated,
            [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
        )
        with self._lock:
            self._latest_jpeg = jpeg.tobytes()

    @staticmethod
    def _blur_score(frame: np.ndarray) -> float:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.Laplacian(gray, cv2.CV_64F).var()

    # ── thread-safe accessors ────────────────────────────────────────────────

    def get_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    def pop_qr_result(self) -> dict:
        """Returns the latest QR result and clears it."""
        with self._lock:
            result = self._qr_result.copy()
            self._qr_result = {}
        return result


# ══════════════════════════════════════════════════════════════════════════════
# HEARTBEAT WATCHDOG
# ══════════════════════════════════════════════════════════════════════════════

class HeartbeatWatchdog:
    """
    Calls on_timeout() when HEARTBEAT_TIMEOUT seconds pass without a ping().
    Resets automatically when a new ping arrives (safe state clears itself).
    """

    def __init__(self, on_timeout):
        self._on_timeout  = on_timeout
        self._last_ping   = time.monotonic()
        self._in_timeout  = False

    def ping(self):
        self._last_ping  = time.monotonic()
        self._in_timeout = False

    def age(self) -> float:
        return time.monotonic() - self._last_ping

    def check(self):
        if not self._in_timeout and self.age() > HEARTBEAT_TIMEOUT:
            self._in_timeout = True
            log.warning(f"[Watchdog] No heartbeat for {self.age():.1f}s — safe state")
            self._on_timeout()


# ══════════════════════════════════════════════════════════════════════════════
# COMMUNICATIONS MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class CommsManager:
    """
    ZeroMQ sockets — rover CONNECTs to middleman which BINDs.

    cmd_sub    SUB  ← commands published by middleman
    telem_push PUSH → telemetry consumed by middleman (PULL)
    video_push PUSH → video frames consumed by middleman (PULL)
    """

    def __init__(self):
        self._ctx = zmq.asyncio.Context()

        self.cmd_sub = self._ctx.socket(zmq.SUB)
        self.cmd_sub.connect(f"tcp://{MIDDLEMAN_HOST}:{CMD_SUB_PORT}")
        self.cmd_sub.setsockopt_string(zmq.SUBSCRIBE, "")  # receive all topics

        self.telem_push = self._ctx.socket(zmq.PUSH)
        self.telem_push.connect(f"tcp://{MIDDLEMAN_HOST}:{TELEMETRY_PORT}")

        self.video_push = self._ctx.socket(zmq.PUSH)
        self.video_push.connect(f"tcp://{MIDDLEMAN_HOST}:{VIDEO_PORT}")

        log.info(
            f"[Comms] ZeroMQ connected → middleman @ {MIDDLEMAN_HOST}  "
            f"(cmd:{CMD_SUB_PORT}, telem:{TELEMETRY_PORT}, video:{VIDEO_PORT})"
        )

    async def recv_command(self) -> Optional[dict]:
        """Non-blocking receive; returns None if no message is waiting."""
        try:
            return await asyncio.wait_for(self.cmd_sub.recv_json(), timeout=0.05)
        except (asyncio.TimeoutError, zmq.ZMQError):
            return None

    async def send_telemetry(self, telem: Telemetry):
        await self.telem_push.send_json(asdict(telem))

    async def send_frame(self, jpeg: bytes):
        # 4-byte big-endian length header so the middleman can split frames cleanly
        header = struct.pack(">I", len(jpeg))
        try:
            await self.video_push.send(header + jpeg, flags=zmq.NOBLOCK)
        except zmq.Again:
            pass  # drop frame if middleman is not keeping up

    def close(self):
        for sock in (self.cmd_sub, self.telem_push, self.video_push):
            sock.close()
        self._ctx.term()


# ══════════════════════════════════════════════════════════════════════════════
# ROVER  — main orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class Rover:
    """
    Owns all subsystems and coordinates them through asyncio tasks.

    Async loops (all run concurrently via asyncio.gather):
      _command_loop    — polls middleman for incoming commands at max speed
      _telemetry_loop  — publishes Telemetry dataclass at ~10 Hz
      _video_loop      — pushes JPEG frames at ~FRAME_RATE Hz
      _watchdog_loop   — checks heartbeat expiry every 250 ms
      _heartbeat_loop  — sends rover-side heartbeat every HEARTBEAT_INTERVAL s
    """

    def __init__(self):
        self._gpio     = GPIO()
        self._motors   = MotorController(self._gpio)
        self._servo    = CameraServo(self._gpio)
        self._ir       = IRSensorManager(self._gpio)
        self._vision   = VisionPipeline(LOG_DIR)
        self._comms    = CommsManager()
        self._state    = RoverState.IDLE
        self._watchdog = HeartbeatWatchdog(on_timeout=self._enter_safe_state)
        log.info("[Rover] All subsystems ready")

    # ── state transitions ────────────────────────────────────────────────────

    def _enter_safe_state(self):
        self._state = RoverState.SAFE_STATE
        self._motors.stop()

    def _enter_operating(self):
        if self._state != RoverState.OPERATING:
            self._state = RoverState.OPERATING
            log.info("[Rover] State → OPERATING")

    # ── command dispatch ─────────────────────────────────────────────────────

    def _apply_command(self, msg: dict):
        """
        Expected message shapes from middleman:

          {"type": "heartbeat"}
          {"type": "drive",  "left": float, "right": float}
          {"type": "servo",  "pulsewidth": int}
          {"type": "stop"}
        """
        kind = msg.get("type")

        if kind == "heartbeat":
            self._watchdog.ping()
            self._enter_operating()

        elif kind == "drive":
            if self._state == RoverState.SAFE_STATE:
                return  # ignore drive commands until heartbeat restored
            left  = float(msg.get("left",  0.0))
            right = float(msg.get("right", 0.0))
            self._motors.set_speeds(left, right)

        elif kind == "servo":
            pw = int(msg.get("pulsewidth", SERVO_MID_PW))
            self._servo.set_pulsewidth(pw)

        elif kind == "stop":
            self._motors.stop()

        else:
            log.debug(f"[Rover] Unrecognised command type: {kind!r}")

    # ── async loops ──────────────────────────────────────────────────────────

    async def _command_loop(self):
        while True:
            msg = await self._comms.recv_command()
            if msg:
                self._apply_command(msg)
            await asyncio.sleep(0)  # yield to other tasks without blocking

    async def _telemetry_loop(self):
        while True:
            ir_front, ir_back = self._ir.read()
            l_spd, r_spd      = self._motors.speeds
            qr                = self._vision.pop_qr_result()

            telem = Telemetry(
                timestamp     = datetime.now().isoformat(timespec="milliseconds"),
                state         = self._state.name,
                left_speed    = round(l_spd, 3),
                right_speed   = round(r_spd, 3),
                servo_pw      = self._servo.pulsewidth,
                ir_front      = ir_front,
                ir_back       = ir_back,
                qr_detected   = bool(qr),
                qr_data       = qr.get("data", ""),
                qr_image_path = qr.get("image_path", ""),
                heartbeat_age = round(self._watchdog.age(), 2),
            )
            await self._comms.send_telemetry(telem)
            await asyncio.sleep(0.1)  # ~10 Hz

    async def _video_loop(self):
        interval = 1.0 / FRAME_RATE
        while True:
            jpeg = self._vision.get_jpeg()
            if jpeg:
                await self._comms.send_frame(jpeg)
            await asyncio.sleep(interval)

    async def _watchdog_loop(self):
        while True:
            self._watchdog.check()
            await asyncio.sleep(0.25)

    async def _heartbeat_loop(self):
        """Lets the middleman know the rover is alive."""
        while True:
            msg = {
                "type":      "rover_heartbeat",
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            }
            await self._comms.telem_push.send_json(msg)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ── entry point ──────────────────────────────────────────────────────────

    async def run(self):
        self._vision.start()
        self._state = RoverState.OPERATING
        log.info("[Rover] Mission started — all loops running")
        try:
            await asyncio.gather(
                self._command_loop(),
                self._telemetry_loop(),
                self._video_loop(),
                self._watchdog_loop(),
                self._heartbeat_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            self._shutdown()

    def _shutdown(self):
        log.info("[Rover] Shutting down")
        self._motors.stop()
        self._vision.stop()
        self._comms.close()
        self._gpio.close()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    rover = Rover()
    try:
        asyncio.run(rover.run())
    except KeyboardInterrupt:
        log.info("[Rover] Stopped by user (Ctrl+C)")
