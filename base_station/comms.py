import json
import logging
import platform
import struct
import subprocess
import threading
import time
from typing import Optional

import cv2
import numpy as np
import zmq

from config import (
    CMD_PUB_PORT,
    CMD_SEND_HZ,
    HEARTBEAT_SEND_HZ,
    LINK_TIMEOUT_S,
    ROVER_IP,
    SERVO_MAX_PW,
    SERVO_MID_PW,
    SERVO_MIN_PW,
    SPEED_RAMP_STEP,
    TELEM_SUB_PORT,
    VIDEO_SUB_PORT,
)
from logger import MissionLogger
from shared_state import AppState

_log = logging.getLogger("comms")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def throttle_steering_to_diff(throttle: float, steering: float) -> tuple[float, float]:
    left  = _clamp(throttle + steering, -1.0, 1.0)
    right = _clamp(throttle - steering, -1.0, 1.0)
    return left, right


def ethernet_available() -> bool:
    system = platform.system()
    try:
        if system == "Linux":
            result = subprocess.run(
                ["ip", "link", "show", "eth0"],
                capture_output=True, text=True, timeout=1.0,
            )
            return "state UP" in result.stdout
        elif system == "Windows":
            result = subprocess.run(
                ["netsh", "interface", "show", "interface"],
                capture_output=True, text=True, timeout=1.0,
            )
            return "Ethernet" in result.stdout and "Connected" in result.stdout
        elif system == "Darwin":
            result = subprocess.run(
                ["ifconfig", "en0"],
                capture_output=True, text=True, timeout=1.0,
            )
            return "status: active" in result.stdout
    except Exception:
        pass
    return False


# ── CommsManager ─────────────────────────────────────────────────────────────

class CommsManager:
    def __init__(self):
        self._ctx: Optional[zmq.Context] = None
        self._cmd_pub: Optional[zmq.Socket] = None
        self._telem_sub: Optional[zmq.Socket] = None
        self._video_sub: Optional[zmq.Socket] = None
        self._running = False
        self._threads: list[threading.Thread] = []
        self._state: Optional[AppState] = None
        self._b3: Optional[MissionLogger] = None
        self._prev_left = 0.0
        self._prev_right = 0.0
        self._servo_pw = SERVO_MID_PW
        self._heartbeat_seq = 0

    def start(self, state: AppState, b3_logger: MissionLogger) -> None:
        self._state = state
        self._b3 = b3_logger
        self._running = True

        self._ctx = zmq.Context()

        # cmd_pub: PUB CONNECTs to rover SUB on :5556
        self._cmd_pub = self._ctx.socket(zmq.PUB)
        self._apply_socket_opts(self._cmd_pub)
        self._cmd_pub.connect(f"tcp://{ROVER_IP}:{CMD_PUB_PORT}")

        # telem_sub: SUB CONNECTs to rover PUB on :5557
        self._telem_sub = self._ctx.socket(zmq.SUB)
        self._apply_socket_opts(self._telem_sub)
        self._telem_sub.setsockopt_string(zmq.SUBSCRIBE, "")  # receive all messages
        self._telem_sub.connect(f"tcp://{ROVER_IP}:{TELEM_SUB_PORT}")

        # video_sub: SUB CONNECTs to rover PUB on :5558
        self._video_sub = self._ctx.socket(zmq.SUB)
        self._apply_socket_opts(self._video_sub)
        self._video_sub.setsockopt_string(zmq.SUBSCRIBE, "")  # receive all messages
        self._video_sub.connect(f"tcp://{ROVER_IP}:{VIDEO_SUB_PORT}")

        thread_specs = [
            ("command_tx",   self._command_tx),
            ("heartbeat_tx", self._heartbeat_tx),
            ("telemetry_rx", self._telemetry_rx),
            ("video_rx",     self._video_rx),
            ("link_monitor", self._link_monitor),
        ]
        for name, target in thread_specs:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        _log.info("CommsManager started — rover at %s", ROVER_IP)

    def stop(self) -> None:
        self._running = False
        for t in self._threads:
            t.join(timeout=2.0)
        for sock in (self._cmd_pub, self._telem_sub, self._video_sub):
            if sock:
                sock.close()
        if self._ctx:
            self._ctx.term()
        _log.info("CommsManager stopped")

    @property
    def status(self) -> dict:
        if self._state is None:
            return {}
        with self._state.state_lock:
            return {
                "packets_rx":     self._state.link.packets_rx,
                "packets_tx":     self._state.link.packets_tx,
                "link_losses":    self._state.link.link_losses,
                "link_ok":        self._state.telemetry.link_ok,
                "cmd_latency_ms": self._state.link.cmd_latency_ms,
                "using_ethernet": self._state.link.using_ethernet,
            }

    @staticmethod
    def _apply_socket_opts(sock: zmq.Socket) -> None:
        sock.setsockopt(zmq.LINGER,          0)
        sock.setsockopt(zmq.SNDHWM,          2)
        sock.setsockopt(zmq.RCVHWM,          2)
        sock.setsockopt(zmq.TCP_KEEPALIVE,   1)

    # ── _command_tx ──────────────────────────────────────────────────────────

    @staticmethod
    def _apply_ramp(prev: float, target: float) -> float:
        diff = target - prev
        if abs(diff) > SPEED_RAMP_STEP:
            return prev + SPEED_RAMP_STEP * (1.0 if diff > 0 else -1.0)
        return target

    def _command_tx(self) -> None:
        interval = 1.0 / CMD_SEND_HZ
        while self._running:
            t0 = time.monotonic()
            state = self._state

            with state.cmd_lock:
                e_stop      = state.command.e_stop
                throttle    = state.command.throttle
                steering    = state.command.steering
                servo_delta = state.command.servo_delta
                # NOTE: capture flag is NOT consumed here — qr_pipeline.py owns that
                state.command.servo_delta = 0  # consume servo delta after reading

            if e_stop:
                msg = json.dumps({"type": "stop"}).encode()
                try:
                    self._cmd_pub.send(msg, zmq.NOBLOCK)
                    with state.state_lock:
                        state.link.packets_tx += 1
                except zmq.ZMQError:
                    pass
                # Reset ramp so there's no lurch when E-Stop is released
                self._prev_left = 0.0
                self._prev_right = 0.0
            else:
                target_left, target_right = throttle_steering_to_diff(throttle, steering)
                new_left  = self._apply_ramp(self._prev_left,  target_left)
                new_right = self._apply_ramp(self._prev_right, target_right)
                self._prev_left  = new_left
                self._prev_right = new_right

                msg = json.dumps({
                    "type":  "drive",
                    "left":  round(new_left,  4),
                    "right": round(new_right, 4),
                    "ts":    time.time(),
                }).encode()
                try:
                    self._cmd_pub.send(msg, zmq.NOBLOCK)
                    with state.state_lock:
                        state.link.packets_tx += 1
                except zmq.ZMQError:
                    pass

            if servo_delta != 0:
                self._servo_pw = int(_clamp(
                    self._servo_pw + servo_delta,
                    SERVO_MIN_PW, SERVO_MAX_PW,
                ))
                servo_msg = json.dumps({
                    "type":       "servo",
                    "pulsewidth": self._servo_pw,
                }).encode()
                try:
                    self._cmd_pub.send(servo_msg, zmq.NOBLOCK)
                    with state.state_lock:
                        state.link.packets_tx += 1
                except zmq.ZMQError:
                    pass

            elapsed = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    # ── _heartbeat_tx ────────────────────────────────────────────────────────

    def _heartbeat_tx(self) -> None:
        interval = 1.0 / HEARTBEAT_SEND_HZ
        while self._running:
            msg = json.dumps({
                "type": "heartbeat",
                "seq":  self._heartbeat_seq,
                "ts":   time.time(),
            }).encode()
            self._heartbeat_seq += 1
            try:
                self._cmd_pub.send(msg, zmq.NOBLOCK)
            except zmq.ZMQError:
                pass
            time.sleep(interval)

    # ── _telemetry_rx ────────────────────────────────────────────────────────

    def _telemetry_rx(self) -> None:
        state = self._state
        while self._running:
            try:
                if not self._telem_sub.poll(timeout=100):
                    continue
                raw = self._telem_sub.recv(zmq.NOBLOCK)
                data = json.loads(raw.decode())
                now = time.time()

                with state.state_lock:
                    t = state.telemetry
                    t.battery_v     = float(data.get("battery_v",     t.battery_v))
                    t.temp_c        = float(data.get("temp_c",        t.temp_c))
                    t.uptime_s      = float(data.get("uptime_s",      t.uptime_s))
                    t.rover_state   = str(  data.get("state",         t.rover_state))
                    t.left_speed    = float(data.get("left_speed",    t.left_speed))
                    t.right_speed   = float(data.get("right_speed",   t.right_speed))
                    t.servo_pw      = int(  data.get("servo_pw",      t.servo_pw))
                    t.ir_front      = bool( data.get("ir_front",      t.ir_front))
                    t.ir_rear       = bool( data.get("ir_rear",       t.ir_rear))
                    t.qr_detected   = bool( data.get("qr_detected",   t.qr_detected))
                    t.qr_data       = str(  data.get("qr_data",       t.qr_data))
                    t.heartbeat_age = float(data.get("heartbeat_age", t.heartbeat_age))
                    t.last_rx       = now
                    state.link.packets_rx += 1

                    if "ts" in data:
                        latency_ms = (now - float(data["ts"])) * 1000.0
                        if latency_ms >= 0:
                            state.link.cmd_latency_ms = latency_ms

            except zmq.ZMQError as e:
                _log.debug("telemetry_rx ZMQError: %s", e)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                _log.warning("telemetry_rx parse error: %s", e)

    # ── _video_rx ────────────────────────────────────────────────────────────

    def _video_rx(self) -> None:
        state = self._state
        while self._running:
            try:
                if not self._video_sub.poll(timeout=100):
                    continue
                raw = self._video_sub.recv(zmq.NOBLOCK)
                if len(raw) < 4:
                    continue
                length = struct.unpack(">I", raw[:4])[0]
                jpeg_bytes = raw[4:4 + length]
                if len(jpeg_bytes) < length:
                    _log.debug("video_rx: truncated frame (%d/%d)", len(jpeg_bytes), length)
                    continue
                arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    with state.frame_lock:
                        state.latest_frame = frame
            except zmq.ZMQError as e:
                _log.debug("video_rx ZMQError: %s", e)
            except Exception as e:
                _log.warning("video_rx error: %s", e)

    # ── _link_monitor ────────────────────────────────────────────────────────

    def _link_monitor(self) -> None:
        state = self._state
        b3    = self._b3
        while self._running:
            time.sleep(0.5)
            now = time.time()

            with state.state_lock:
                last_rx  = state.telemetry.last_rx
                was_ok   = state.telemetry.link_ok
                n_losses = state.link.link_losses

            if last_rx == 0.0:
                continue  # no packets ever received — nothing to monitor yet

            age = now - last_rx

            if age > LINK_TIMEOUT_S and was_ok:
                with state.state_lock:
                    state.telemetry.link_ok  = False
                    state.link.link_losses  += 1
                state.add_alert("LINK LOST")
                b3.log_action("LINK_LOST")
                _log.warning("Link lost — no telemetry for %.1f s", age)

                if ethernet_available():
                    state.add_alert("Ethernet fallback available")
                    b3.log_action("ETH_FALLBACK")
                    with state.state_lock:
                        state.link.using_ethernet = True

            elif age <= LINK_TIMEOUT_S and not was_ok:
                with state.state_lock:
                    state.telemetry.link_ok = True
                state.clear_alert("LINK LOST")
                b3.log_action("LINK_RESTORED", f"losses={n_losses}")
                _log.info("Link restored after %d loss(es)", n_losses)


# ── Standalone loopback test ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-14s  %(levelname)-7s  %(message)s",
    )

    # Override rover IP to loopback so we can test without a real rover.
    # Rebind the module-level ROVER_IP directly (imported from config at module top).
    global ROVER_IP   # noqa: PLW0603
    ROVER_IP = "127.0.0.1"

    # Minimal stand-in for MissionLogger
    class _MockLogger:
        def log_action(self, action: str, detail: str = "") -> None:
            _log.info("[MockLogger] %s  %s", action, detail)

    def _mock_rover_thread() -> None:
        """Binds on rover ports and pushes fake telemetry + dummy video."""
        ctx = zmq.Context()

        # Use PUB so the base station's SUB sockets can connect to them
        telem_push = ctx.socket(zmq.PUB)
        telem_push.setsockopt(zmq.LINGER, 0)
        telem_push.bind(f"tcp://*:{TELEM_SUB_PORT}")

        video_push = ctx.socket(zmq.PUB)
        video_push.setsockopt(zmq.LINGER, 0)
        video_push.bind(f"tcp://*:{VIDEO_SUB_PORT}")

        cmd_sub = ctx.socket(zmq.SUB)
        cmd_sub.setsockopt(zmq.LINGER, 0)
        cmd_sub.setsockopt_string(zmq.SUBSCRIBE, "")
        cmd_sub.bind(f"tcp://*:{CMD_PUB_PORT}")

        seq = 0
        while True:
            telem = {
                "battery_v":     max(6.0, 8.4 - seq * 0.002),
                "temp_c":        45.0 + (seq % 30) * 0.5,
                "uptime_s":      seq * 0.1,
                "state":         "OPERATING",
                "left_speed":    0.0,
                "right_speed":   0.0,
                "servo_pw":      1500,
                "ir_front":      False,
                "ir_rear":       False,
                "qr_detected":   False,
                "qr_data":       "",
                "heartbeat_age": 0.3,
                "ts":            time.time(),
            }
            try:
                telem_push.send(json.dumps(telem).encode(), zmq.NOBLOCK)
            except zmq.ZMQError:
                pass

            # Synthetic 4×4 JPEG frame
            if seq % 3 == 0:
                dummy = np.zeros((4, 4, 3), dtype=np.uint8)
                ok, buf = cv2.imencode(".jpg", dummy, [cv2.IMWRITE_JPEG_QUALITY, 50])
                if ok:
                    jpeg = buf.tobytes()
                    packet = struct.pack(">I", len(jpeg)) + jpeg
                    try:
                        video_push.send(packet, zmq.NOBLOCK)
                    except zmq.ZMQError:
                        pass

            # Drain incoming commands and print them
            try:
                while True:
                    raw_cmd = cmd_sub.recv(zmq.NOBLOCK)
                    _log.info("[MockRover] cmd: %s", raw_cmd.decode()[:80])
            except zmq.ZMQError:
                pass

            seq += 1
            time.sleep(0.1)

    rover_t = threading.Thread(target=_mock_rover_thread, daemon=True)
    rover_t.start()
    time.sleep(0.3)  # wait for mock rover to bind

    state   = AppState()
    mock_b3 = _MockLogger()
    comms   = CommsManager()
    comms.start(state, mock_b3)

    print("\nLoopback test running for 10 s — link stats printed every 2 s\n")
    for i in range(5):
        time.sleep(2.0)
        s = comms.status
        print(
            f"t={2*(i+1):2d}s  "
            f"rx={s['packets_rx']:4d}  tx={s['packets_tx']:4d}  "
            f"link={'OK' if s['link_ok'] else 'LOST':4s}  "
            f"lat={s['cmd_latency_ms']:.1f}ms  "
            f"batt={state.telemetry.battery_v:.2f}V  "
            f"uptime={state.telemetry.uptime_s:.1f}s"
        )

    comms.stop()
    print("\nTest complete.")
