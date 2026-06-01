"""
rover/rover.py — ORT Rover Software
Queen Mary University of London (QMSEDS)
UKSEDS Olympus Rover Trials 2025–2026
"""
import asyncio
import json
import logging
import logging.handlers
import struct
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
import zmq

from config import (
    BASE_STATION_IP,
    BATTERY_CHANNEL,
    BATTERY_DIVIDER_R1,
    BATTERY_DIVIDER_R2,
    BATTERY_I2C_ADDRESS,
    CAMERA_INDEX,
    CMD_SUB_PORT,
    FRAME_HEIGHT,
    FRAME_RATE,
    FRAME_WIDTH,
    HEARTBEAT_SEND_HZ,
    HEARTBEAT_TIMEOUT_S,
    IR_FRONT_PIN,
    IR_REAR_PIN,
    JPEG_QUALITY,
    LEFT_FRONT_DIR,
    LEFT_FRONT_PWM,
    LEFT_REAR_DIR,
    LEFT_REAR_PWM,
    MAX_SPEED,
    MOCK_MODE,
    OBSTACLE_BLOCK_DRIVE,
    PWM_FREQUENCY,
    QR_BLUR_THRESHOLD,
    RIGHT_FRONT_DIR,
    RIGHT_FRONT_PWM,
    RIGHT_REAR_DIR,
    RIGHT_REAR_PWM,
    SERVO_MAX_PW,
    SERVO_MID_PW,
    SERVO_MIN_PW,
    SERVO_PIN,
    TELEM_PUSH_PORT,
    VIDEO_PUSH_PORT,
)

# ── Mission log directory ──────────────────────────────────────────────────────
LOG_DIR = Path("mission_logs") / datetime.now().strftime("%Y%m%d_%H%M%S")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler()],
)


# ── Rover state ────────────────────────────────────────────────────────────────
class RoverState(Enum):
    IDLE       = "IDLE"
    OPERATING  = "OPERATING"
    SAFE_STATE = "SAFE_STATE"
    ERROR      = "ERROR"


# ── Telemetry dataclass ────────────────────────────────────────────────────────
@dataclass
class Telemetry:
    timestamp:     str   = ""
    state:         str   = "IDLE"
    left_speed:    float = 0.0
    right_speed:   float = 0.0
    servo_pw:      int   = 1500
    ir_front:      bool  = False
    ir_rear:       bool  = False
    battery_v:     float = 0.0
    temp_c:        float = 0.0
    uptime_s:      float = 0.0
    qr_detected:   bool  = False
    qr_data:       str   = ""
    heartbeat_age: float = 0.0
    ts:            float = 0.0   # unix timestamp for base-station latency measurement


# ── CPU temperature ────────────────────────────────────────────────────────────
def read_cpu_temp() -> float:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000.0
    except Exception:
        return 0.0


# ── Hardware abstraction layer ─────────────────────────────────────────────────
class GPIO:
    """Wraps pigpio in real mode. Silent no-ops in MOCK_MODE."""

    def __init__(self):
        self._pi = None
        self._log = logging.getLogger("gpio")
        if not MOCK_MODE:
            import pigpio  # type: ignore
            self._pi = pigpio.pi()
            if not self._pi.connected:
                raise RuntimeError("pigpio daemon not running — start with: sudo pigpiod")
            self._log.info("pigpio connected")
        else:
            self._log.info("MOCK_MODE — hardware calls disabled")

    def mode_output(self, pin: int) -> None:
        if not MOCK_MODE:
            import pigpio  # type: ignore
            self._pi.set_mode(pin, pigpio.OUTPUT)

    def mode_input(self, pin: int) -> None:
        if not MOCK_MODE:
            import pigpio  # type: ignore
            self._pi.set_mode(pin, pigpio.INPUT)

    def set_pwm_freq(self, pin: int, freq: int) -> None:
        if not MOCK_MODE:
            self._pi.set_PWM_frequency(pin, freq)

    def set_pwm_duty(self, pin: int, duty_0_255: int) -> None:
        if not MOCK_MODE:
            self._pi.set_PWM_dutycycle(pin, duty_0_255)

    def write(self, pin: int, level: int) -> None:
        if not MOCK_MODE:
            self._pi.write(pin, level)

    def read(self, pin: int) -> int:
        if MOCK_MODE:
            return 1  # 1 = no obstacle (active-low sensor)
        return self._pi.read(pin)

    def set_servo_pw(self, pin: int, pw: int) -> None:
        if not MOCK_MODE:
            self._pi.set_servo_pulsewidth(pin, pw)

    def close(self) -> None:
        if not MOCK_MODE and self._pi:
            self._pi.stop()
            self._log.info("closed")


# ── Motor Controller ───────────────────────────────────────────────────────────
class MotorController:
    def __init__(self, gpio: GPIO):
        self._gpio  = gpio
        self._left  = 0.0
        self._right = 0.0
        self._log   = logging.getLogger("motors")

        for pin in (LEFT_FRONT_PWM, LEFT_REAR_PWM, RIGHT_FRONT_PWM, RIGHT_REAR_PWM):
            self._gpio.mode_output(pin)
            self._gpio.set_pwm_freq(pin, PWM_FREQUENCY)

        for pin in (LEFT_FRONT_DIR, LEFT_REAR_DIR, RIGHT_FRONT_DIR, RIGHT_REAR_DIR):
            self._gpio.mode_output(pin)

        self._log.info("ready")

    def set_speeds(self, left: float, right: float) -> None:
        left  = max(-MAX_SPEED, min(MAX_SPEED, left))
        right = max(-MAX_SPEED, min(MAX_SPEED, right))
        self._left  = left
        self._right = right
        self._drive_side(left,  LEFT_FRONT_PWM,  LEFT_FRONT_DIR,  LEFT_REAR_PWM,  LEFT_REAR_DIR)
        self._drive_side(right, RIGHT_FRONT_PWM, RIGHT_FRONT_DIR, RIGHT_REAR_PWM, RIGHT_REAR_DIR)

    def _drive_side(
        self, speed: float,
        pwm_f: int, dir_f: int,
        pwm_r: int, dir_r: int,
    ) -> None:
        direction = 0 if speed >= 0 else 1
        duty = int(abs(speed) * 255)
        self._gpio.write(dir_f, direction)
        self._gpio.write(dir_r, direction)
        self._gpio.set_pwm_duty(pwm_f, duty)
        self._gpio.set_pwm_duty(pwm_r, duty)

    def stop(self) -> None:
        for pin in (LEFT_FRONT_PWM, LEFT_REAR_PWM, RIGHT_FRONT_PWM, RIGHT_REAR_PWM):
            self._gpio.set_pwm_duty(pin, 0)
        self._left  = 0.0
        self._right = 0.0

    @property
    def speeds(self) -> tuple:
        return (self._left, self._right)


# ── Camera Servo ───────────────────────────────────────────────────────────────
class CameraServo:
    def __init__(self, gpio: GPIO):
        self._gpio = gpio
        self._pw   = SERVO_MID_PW
        self._gpio.set_servo_pw(SERVO_PIN, self._pw)
        logging.getLogger("servo").info("ready at %d µs", self._pw)

    def set_pulsewidth(self, pw: int) -> None:
        self._pw = max(SERVO_MIN_PW, min(SERVO_MAX_PW, pw))
        self._gpio.set_servo_pw(SERVO_PIN, self._pw)

    @property
    def pulsewidth(self) -> int:
        return self._pw


# ── IR Sensor Manager ──────────────────────────────────────────────────────────
class IRSensorManager:
    def __init__(self, gpio: GPIO):
        self._gpio = gpio
        self._gpio.mode_input(IR_FRONT_PIN)
        self._gpio.mode_input(IR_REAR_PIN)
        logging.getLogger("ir").info("ready")

    def read(self) -> tuple:
        # active-low: GPIO reads 0 = obstacle present
        ir_front = (self._gpio.read(IR_FRONT_PIN) == 0)
        ir_rear  = (self._gpio.read(IR_REAR_PIN)  == 0)
        return ir_front, ir_rear


# ── Battery Monitor ────────────────────────────────────────────────────────────
class BatteryMonitor:
    def __init__(self):
        self._log         = logging.getLogger("battery")
        self._mock_start  = time.monotonic()
        self._ads         = None
        self._chan        = None

        if not MOCK_MODE:
            import board                              # type: ignore
            import busio                              # type: ignore
            import adafruit_ads1x15.ads1115 as ADS   # type: ignore
            from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore
            i2c        = busio.I2C(board.SCL, board.SDA)
            self._ads  = ADS.ADS1115(i2c, address=BATTERY_I2C_ADDRESS)
            self._chan  = AnalogIn(self._ads, BATTERY_CHANNEL)

        self._log.info("ready%s", " (mock)" if MOCK_MODE else "")

    def read_voltage(self) -> float:
        if MOCK_MODE:
            # drain from 8.4 V → 6.0 V over 1800 s (~30 min)
            elapsed = time.monotonic() - self._mock_start
            return max(6.0, 8.4 - (elapsed / 1800.0) * 2.4)
        v_adc = self._chan.voltage
        return v_adc * (BATTERY_DIVIDER_R1 + BATTERY_DIVIDER_R2) / BATTERY_DIVIDER_R2


# ── Vision Pipeline ────────────────────────────────────────────────────────────
class VisionPipeline:
    def __init__(self):
        self._log        = logging.getLogger("vision")
        self._lock       = threading.Lock()
        self._jpeg:      Optional[bytes] = None
        self._qr_result: dict            = {}
        self._running    = False
        self._thread:    Optional[threading.Thread] = None

    def start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "captures").mkdir(parents=True, exist_ok=True)
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True, name="vision")
        self._thread.start()
        self._log.info("started (mock=%s)", MOCK_MODE)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        self._log.info("stopped")

    def get_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._jpeg

    def pop_qr_result(self) -> dict:
        with self._lock:
            result = self._qr_result
            self._qr_result = {}
            return result

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        if MOCK_MODE:
            self._run_mock()
        else:
            self._run_real()

    def _run_mock(self) -> None:
        try:
            import qrcode as _qrlib  # type: ignore
            qr_available = True
        except ImportError:
            _qrlib = None
            qr_available = False
            self._log.warning("qrcode library not available — QR embedding disabled")

        qr_sites  = ["MOCK_QR_SITE_01", "MOCK_QR_SITE_02", "MOCK_QR_SITE_03"]
        interval  = 1.0 / FRAME_RATE
        frame_count = 0

        while self._running:
            t0    = time.monotonic()
            frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
            frame[:] = (20, 30, 50)

            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            cv2.putText(frame, f"MOCK ROVER  {ts}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 120), 2)
            cv2.putText(frame, f"Frame {frame_count}", (10, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (150, 150, 200), 1)

            if frame_count % 30 == 0 and qr_available:
                site    = qr_sites[(frame_count // 30) % len(qr_sites)]
                overlay = self._generate_qr_overlay(site, _qrlib)
                if overlay is not None:
                    x = FRAME_WIDTH  // 2 - overlay.shape[1] // 2
                    y = FRAME_HEIGHT // 2 - overlay.shape[0] // 2
                    x2 = min(x + overlay.shape[1], FRAME_WIDTH)
                    y2 = min(y + overlay.shape[0], FRAME_HEIGHT)
                    frame[y:y2, x:x2] = overlay[:y2 - y, :x2 - x]
                    image_path = self._save_capture(frame, site)
                    with self._lock:
                        self._qr_result = {"data": site, "image_path": image_path}

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with self._lock:
                    self._jpeg = buf.tobytes()

            frame_count += 1
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))

    def _run_real(self) -> None:
        from pyzbar.pyzbar import decode as pyzbar_decode  # type: ignore

        cap = cv2.VideoCapture(CAMERA_INDEX)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS,          FRAME_RATE)

        if not cap.isOpened():
            self._log.error("cannot open camera index %d", CAMERA_INDEX)
            return

        interval = 1.0 / FRAME_RATE
        while self._running:
            t0     = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                self._log.warning("camera read failed")
                time.sleep(0.05)
                continue

            grey       = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blur_score = cv2.Laplacian(grey, cv2.CV_64F).var()
            if blur_score >= QR_BLUR_THRESHOLD:
                equalized  = cv2.equalizeHist(grey)
                detections = pyzbar_decode(equalized)
                if detections:
                    qr_text    = detections[0].data.decode("utf-8", errors="replace")
                    image_path = self._save_capture(frame, qr_text)
                    with self._lock:
                        self._qr_result = {"data": qr_text, "image_path": image_path}

            ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok2:
                with self._lock:
                    self._jpeg = buf.tobytes()

            time.sleep(max(0.0, interval - (time.monotonic() - t0)))

        cap.release()

    def _generate_qr_overlay(self, data: str, qrlib) -> Optional[np.ndarray]:
        try:
            from PIL import Image  # type: ignore
            qr = qrlib.QRCode(box_size=3, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        except Exception as e:
            self._log.warning("QR overlay failed: %s", e)
            return None

    def _save_capture(self, frame: np.ndarray, qr_text: str) -> str:
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_text = "".join(c if c.isalnum() or c in "_-" else "_" for c in qr_text)[:20]
        path      = LOG_DIR / "captures" / f"qr_{ts}_{safe_text}.jpg"
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return str(path)


# ── Heartbeat Watchdog ─────────────────────────────────────────────────────────
class HeartbeatWatchdog:
    def __init__(self, on_timeout: Callable, on_restore: Optional[Callable] = None):
        self._on_timeout  = on_timeout
        self._on_restore  = on_restore
        self._last_ping   = time.monotonic()
        self._in_timeout  = False
        self._log         = logging.getLogger("watchdog")

    def ping(self) -> None:
        was_timed_out    = self._in_timeout
        self._last_ping  = time.monotonic()
        self._in_timeout = False
        if was_timed_out:
            self._log.info("heartbeat restored")
            if self._on_restore:
                self._on_restore()

    def age(self) -> float:
        return time.monotonic() - self._last_ping

    def check(self) -> None:
        if self.age() > HEARTBEAT_TIMEOUT_S and not self._in_timeout:
            self._in_timeout = True
            self._log.warning("heartbeat timeout (%.1f s) — entering safe state", self.age())
            self._on_timeout()

    @property
    def is_timed_out(self) -> bool:
        return self._in_timeout


# ── Comms Manager (rover side) ─────────────────────────────────────────────────
class CommsManager:
    """
    Rover BINDs on all three ports. Base station CONNECTs.
    Rover uses SUB for commands, PUB for telemetry + video.
    (Base uses PUB for commands, SUB for telemetry + video.)
    """

    def __init__(self):
        self._log = logging.getLogger("comms")
        self._ctx = zmq.Context()
        self._seq = 0

        self._cmd_sub    = self._ctx.socket(zmq.SUB)
        self._telem_pub  = self._ctx.socket(zmq.PUB)
        self._video_pub  = self._ctx.socket(zmq.PUB)

        for sock in (self._cmd_sub, self._telem_pub, self._video_pub):
            sock.setsockopt(zmq.LINGER,         0)
            sock.setsockopt(zmq.SNDHWM,         2)
            sock.setsockopt(zmq.RCVHWM,         2)
            sock.setsockopt(zmq.TCP_KEEPALIVE,  1)

        self._cmd_sub.setsockopt_string(zmq.SUBSCRIBE, "")
        self._cmd_sub.bind(f"tcp://*:{CMD_SUB_PORT}")
        self._telem_pub.bind(f"tcp://*:{TELEM_PUSH_PORT}")
        self._video_pub.bind(f"tcp://*:{VIDEO_PUSH_PORT}")

        self._log.info(
            "bound — cmd:%d  telem:%d  video:%d",
            CMD_SUB_PORT, TELEM_PUSH_PORT, VIDEO_PUSH_PORT,
        )

    def recv_command(self) -> Optional[dict]:
        try:
            raw = self._cmd_sub.recv(flags=zmq.NOBLOCK)
            return json.loads(raw)
        except zmq.Again:
            return None
        except Exception as e:
            self._log.warning("recv_command error: %s", e)
            return None

    def send_telemetry(self, telem: Telemetry) -> None:
        try:
            self._telem_pub.send_string(json.dumps(asdict(telem)), flags=zmq.NOBLOCK)
        except zmq.Again:
            pass
        except Exception as e:
            self._log.warning("send_telemetry error: %s", e)

    def send_frame(self, jpeg: bytes) -> None:
        """Prepend 4-byte big-endian length header, then publish."""
        try:
            packet = struct.pack(">I", len(jpeg)) + jpeg
            self._video_pub.send(packet, flags=zmq.NOBLOCK)
        except zmq.Again:
            pass
        except Exception as e:
            self._log.warning("send_frame error: %s", e)

    def send_heartbeat(self) -> None:
        self._seq += 1
        msg = json.dumps({"type": "heartbeat", "seq": self._seq, "ts": time.time()})
        try:
            self._telem_pub.send_string(msg, flags=zmq.NOBLOCK)
        except zmq.Again:
            pass

    def close(self) -> None:
        for sock in (self._cmd_sub, self._telem_pub, self._video_pub):
            sock.close()
        self._ctx.term()
        self._log.info("closed")


# ── Rover (main orchestrator) ──────────────────────────────────────────────────
class Rover:
    def __init__(self):
        self._log        = logging.getLogger("rover")
        self._state      = RoverState.IDLE
        self._start_time = time.monotonic()
        self._running    = False

        # deduplicate obstacle-blocked log messages
        self._obstacle_front_logged = False
        self._obstacle_rear_logged  = False

        self._log.info("initialising subsystems…")
        self.gpio     = GPIO()
        self.motors   = MotorController(self.gpio)
        self.servo    = CameraServo(self.gpio)
        self.ir       = IRSensorManager(self.gpio)
        self.battery  = BatteryMonitor()
        self.vision   = VisionPipeline()
        self.comms    = CommsManager()
        self.watchdog = HeartbeatWatchdog(
            on_timeout=self._enter_safe_state,
            on_restore=self._exit_safe_state,
        )

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._log.info("all subsystems ready. LOG_DIR=%s", LOG_DIR)

    # ── state transitions ──────────────────────────────────────────────────────

    def _enter_safe_state(self) -> None:
        self._state = RoverState.SAFE_STATE
        self.motors.stop()
        self._log.warning("SAFE STATE — motors stopped")

    def _exit_safe_state(self) -> None:
        self._state = RoverState.OPERATING
        self._log.info("heartbeat restored — returning to OPERATING")

    # ── command dispatch ───────────────────────────────────────────────────────

    def _apply_command(self, cmd: dict) -> None:
        cmd_type = cmd.get("type")

        if cmd_type == "heartbeat":
            self.watchdog.ping()
            return

        if cmd_type == "stop":
            self.motors.stop()
            return

        if cmd_type == "servo":
            pw = int(cmd.get("pulsewidth", SERVO_MID_PW))
            self.servo.set_pulsewidth(pw)
            return

        if cmd_type == "drive":
            if self._state == RoverState.SAFE_STATE:
                return  # drive blocked until heartbeat restored

            left  = float(cmd.get("left",  0.0))
            right = float(cmd.get("right", 0.0))

            if OBSTACLE_BLOCK_DRIVE:
                ir_front, ir_rear = self.ir.read()

                if ir_front and left > 0 and right > 0:
                    if not self._obstacle_front_logged:
                        self._log.warning("OBSTACLE_FRONT — forward drive blocked")
                        self._obstacle_front_logged = True
                    return
                else:
                    self._obstacle_front_logged = False

                if ir_rear and left < 0 and right < 0:
                    if not self._obstacle_rear_logged:
                        self._log.warning("OBSTACLE_REAR — reverse drive blocked")
                        self._obstacle_rear_logged = True
                    return
                else:
                    self._obstacle_rear_logged = False

            self.motors.set_speeds(left, right)

            if self._state != RoverState.OPERATING:
                self._state = RoverState.OPERATING

    # ── async loops ───────────────────────────────────────────────────────────

    async def _command_loop(self) -> None:
        while self._running:
            cmd = self.comms.recv_command()
            if cmd:
                self._apply_command(cmd)
            await asyncio.sleep(0)  # yield to event loop

    async def _telemetry_loop(self) -> None:
        interval = 1.0 / 10  # 10 Hz
        while self._running:
            ir_front, ir_rear = self.ir.read()
            qr = self.vision.pop_qr_result()
            telem = Telemetry(
                timestamp     = datetime.now().isoformat(),
                state         = self._state.value,
                left_speed    = self.motors.speeds[0],
                right_speed   = self.motors.speeds[1],
                servo_pw      = self.servo.pulsewidth,
                ir_front      = ir_front,
                ir_rear       = ir_rear,
                battery_v     = self.battery.read_voltage(),
                temp_c        = read_cpu_temp(),
                uptime_s      = time.monotonic() - self._start_time,
                qr_detected   = bool(qr.get("data")),
                qr_data       = qr.get("data", ""),
                heartbeat_age = self.watchdog.age(),
                ts            = time.time(),
            )
            self.comms.send_telemetry(telem)
            await asyncio.sleep(interval)

    async def _video_loop(self) -> None:
        interval = 1.0 / FRAME_RATE
        while self._running:
            jpeg = self.vision.get_jpeg()
            if jpeg:
                self.comms.send_frame(jpeg)
            await asyncio.sleep(interval)

    async def _watchdog_loop(self) -> None:
        while self._running:
            self.watchdog.check()
            await asyncio.sleep(0.25)

    async def _heartbeat_loop(self) -> None:
        interval = 1.0 / HEARTBEAT_SEND_HZ
        while self._running:
            self.comms.send_heartbeat()
            await asyncio.sleep(interval)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._state   = RoverState.OPERATING
        self.vision.start()
        self._log.info("entering main event loop")
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

    def shutdown(self) -> None:
        self._log.info("shutdown requested")
        self._running = False
        self.motors.stop()
        self.vision.stop()
        self.comms.close()
        self.gpio.close()
        self._log.info("shutdown complete")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.getLogger().addHandler(
        logging.FileHandler(LOG_DIR / "rover.log")
    )
    rover = Rover()
    try:
        asyncio.run(rover.run())
    except KeyboardInterrupt:
        logging.getLogger("rover").info("KeyboardInterrupt — shutting down")
    finally:
        rover.shutdown()
