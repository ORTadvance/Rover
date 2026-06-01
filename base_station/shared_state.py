import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config import OBSTACLE_BLOCK_DRIVE


@dataclass
class RoverTelemetry:
    battery_v:     float = 0.0
    temp_c:        float = 0.0
    uptime_s:      float = 0.0
    rover_state:   str   = "IDLE"
    left_speed:    float = 0.0
    right_speed:   float = 0.0
    servo_pw:      int   = 1500
    ir_front:      bool  = False
    ir_rear:       bool  = False
    qr_detected:   bool  = False
    qr_data:       str   = ""
    heartbeat_age: float = 0.0
    link_ok:       bool  = False
    last_rx:       float = 0.0


@dataclass
class DriveCommand:
    throttle:    float = 0.0
    steering:    float = 0.0
    e_stop:      bool  = False
    capture:     bool  = False
    servo_delta: int   = 0


@dataclass
class LinkStats:
    packets_rx:     int   = 0
    packets_tx:     int   = 0
    link_losses:    int   = 0
    using_ethernet: bool  = False
    cmd_latency_ms: float = 0.0


class AppState:
    def __init__(self):
        self.telemetry   = RoverTelemetry()
        self.command     = DriveCommand()
        self.link        = LinkStats()
        self.latest_frame: Optional[np.ndarray] = None
        self.qr_overlay:   Optional[str]        = None
        self.qr_history:   list[dict]           = []
        self.alerts:       list[str]            = []
        self.obstacle_block_enabled: bool       = OBSTACLE_BLOCK_DRIVE
        self.frame_lock  = threading.Lock()
        self.cmd_lock    = threading.Lock()
        self.state_lock  = threading.Lock()

    def add_alert(self, msg: str):
        with self.state_lock:
            if msg not in self.alerts:
                self.alerts.append(msg)

    def clear_alert(self, msg: str):
        with self.state_lock:
            self.alerts = [a for a in self.alerts if a != msg]

    def add_qr(self, text: str, image_path: str, timestamp_str: str):
        with self.state_lock:
            self.qr_history.append({
                "text":          text,
                "image_path":    image_path,
                "timestamp_str": timestamp_str,
            })
            self.qr_overlay = text


if __name__ == "__main__":
    state = AppState()

    state.add_alert("LINK LOST")
    state.add_alert("BATTERY LOW")
    state.add_alert("LINK LOST")  # duplicate — should not be added
    assert len(state.alerts) == 2, f"Expected 2 alerts, got {len(state.alerts)}"

    state.clear_alert("LINK LOST")
    assert state.alerts == ["BATTERY LOW"], f"Unexpected alerts: {state.alerts}"

    state.add_qr("SITE_ALPHA_42", "captures/still_1.jpg", "2026-01-01 12:03:15")
    state.add_qr("SITE_BETA_07",  "captures/still_2.jpg", "2026-01-01 12:07:44")
    assert len(state.qr_history) == 2
    assert state.qr_overlay == "SITE_BETA_07"

    errors = []

    def writer(idx):
        try:
            for _ in range(50):
                state.add_qr(f"QR_{idx}", f"captures/{idx}.jpg", "2026-01-01 12:00:00")
                state.add_alert(f"ALERT_{idx}")
                state.clear_alert(f"ALERT_{idx}")
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread errors: {errors}"

    print("All shared_state tests passed.")
    print(f"  qr_history entries: {len(state.qr_history)}")
    print(f"  alerts:             {state.alerts}")
