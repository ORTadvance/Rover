"""
B1 - Operator Interface Module
UKSEDS ORT Base Station Software
================================
Handles:
  - Live video feed display (receives JPEG frames via UDP from rover)
  - Keyboard + gamepad control input
  - Telemetry display (battery, link status, uptime)
  - Mission timer (30-minute countdown)
  - Status alerts
  - Publishes drive commands via UDP to rover (integrates with B4)

Dependencies:
    pip install pygame opencv-python-headless numpy

Usage:
    python b1_operator_interface.py --rover-ip 192.168.1.100
"""

import pygame
import socket
import threading
import time
import json
import struct
import argparse
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
import cv2

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

ROVER_IP_DEFAULT    = "192.168.1.100"
CMD_PORT            = 5000        # UDP port to SEND drive commands to rover
TELEMETRY_PORT      = 5001        # UDP port to RECEIVE telemetry from rover
VIDEO_PORT          = 5002        # UDP port to RECEIVE JPEG video frames from rover
CMD_RATE_HZ         = 20          # How often to send drive commands (Hz)
MISSION_DURATION_S  = 30 * 60    # 30-minute mission window

# Display
WINDOW_W, WINDOW_H  = 1280, 720
VIDEO_W,  VIDEO_H   = 854, 480   # Left panel video area
PANEL_W             = WINDOW_W - VIDEO_W  # Right panel width = 426px

# Colours (dark aerospace theme)
C_BG          = (10,  12,  18)
C_PANEL       = (18,  22,  32)
C_BORDER      = (40,  50,  70)
C_ACCENT      = (0,  200, 120)   # Green
C_WARN        = (255, 180,  30)  # Amber
C_DANGER      = (220,  50,  50)  # Red
C_TEXT        = (210, 220, 235)
C_SUBTEXT     = (100, 120, 150)
C_VIDEO_BG    = (5,    8,  14)

# Drive command values sent to rover
DRIVE_SPEED   = 0.6   # 0.0 – 1.0 normalised speed
TURN_SPEED    = 0.5


# ─────────────────────────────────────────────
# Shared State
# ─────────────────────────────────────────────

@dataclass
class Telemetry:
    battery_v: float      = 0.0
    link_ok: bool         = False
    uptime_s: float       = 0.0
    temp_c: float         = 0.0
    last_rx: float        = field(default_factory=time.time)

@dataclass
class DriveCommand:
    throttle: float = 0.0   # -1.0 (reverse) to +1.0 (forward)
    steering: float = 0.0   # -1.0 (left)    to +1.0 (right)
    stop: bool      = False
    capture: bool   = False  # Trigger still-image capture (→ B2)

@dataclass
class AppState:
    telemetry:      Telemetry    = field(default_factory=Telemetry)
    command:        DriveCommand = field(default_factory=DriveCommand)
    mission_start:  float        = field(default_factory=time.time)
    mission_active: bool         = True
    latest_frame:   Optional[np.ndarray] = None
    alerts:         list         = field(default_factory=list)
    qr_overlay:     Optional[str] = None   # Last decoded QR text (from B2)
    frame_lock:     threading.Lock = field(default_factory=threading.Lock)
    cmd_lock:       threading.Lock = field(default_factory=threading.Lock)


# ─────────────────────────────────────────────
# Networking Threads
# ─────────────────────────────────────────────

def telemetry_receiver(state: AppState, port: int):
    """Receive JSON telemetry datagrams from rover."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", port))
    sock.settimeout(1.0)
    while state.mission_active:
        try:
            data, _ = sock.recvfrom(1024)
            payload = json.loads(data.decode())
            t = state.telemetry
            t.battery_v = payload.get("battery_v", t.battery_v)
            t.uptime_s  = payload.get("uptime_s",  t.uptime_s)
            t.temp_c    = payload.get("temp_c",    t.temp_c)
            t.link_ok   = True
            t.last_rx   = time.time()
        except socket.timeout:
            pass
        except Exception:
            pass
        # Mark link lost if no packet for 3 seconds
        if time.time() - state.telemetry.last_rx > 3.0:
            state.telemetry.link_ok = False
    sock.close()


def video_receiver(state: AppState, port: int):
    """
    Receive JPEG video frames from rover.
    Protocol: 4-byte big-endian length header + JPEG bytes.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", port))
    sock.settimeout(1.0)
    MAX_DGRAM = 65507
    while state.mission_active:
        try:
            data, _ = sock.recvfrom(MAX_DGRAM)
            if len(data) < 4:
                continue
            length = struct.unpack(">I", data[:4])[0]
            jpeg   = data[4:4 + length]
            arr    = np.frombuffer(jpeg, dtype=np.uint8)
            frame  = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is not None:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                with state.frame_lock:
                    state.latest_frame = frame
        except socket.timeout:
            pass
        except Exception:
            pass
    sock.close()


def command_sender(state: AppState, rover_ip: str, port: int):
    """Send drive commands to rover at CMD_RATE_HZ."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / CMD_RATE_HZ
    while state.mission_active:
        with state.cmd_lock:
            cmd = state.command
            payload = json.dumps({
                "throttle": round(cmd.throttle, 3),
                "steering": round(cmd.steering, 3),
                "stop":     cmd.stop,
                "capture":  cmd.capture,
                "ts":       time.time(),
            }).encode()
            # Reset single-shot flags
            state.command.capture = False
        try:
            sock.sendto(payload, (rover_ip, port))
        except Exception:
            pass
        time.sleep(interval)
    sock.close()


# ─────────────────────────────────────────────
# Input Handling
# ─────────────────────────────────────────────

def read_inputs(state: AppState, joystick: Optional[pygame.joystick.Joystick],
                keys, events) -> bool:
    """
    Update DriveCommand from keyboard + optional gamepad.
    Returns False if the operator wants to quit.
    """
    for event in events:
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                return False
            if event.key == pygame.K_SPACE:
                with state.cmd_lock:
                    state.command.capture = True   # Trigger still capture → B2

    throttle = 0.0
    steering = 0.0
    stop     = False

    # ── Gamepad (primary) ──────────────────────────────────────────────
    if joystick:
        # Left stick Y-axis → throttle (axis 1 is typically inverted)
        raw_throttle = -joystick.get_axis(1)
        raw_steering =  joystick.get_axis(0)
        # Deadzone
        if abs(raw_throttle) < 0.08:
            raw_throttle = 0.0
        if abs(raw_steering) < 0.08:
            raw_steering = 0.0
        throttle = raw_throttle * DRIVE_SPEED
        steering = raw_steering * TURN_SPEED

        # Button 0 (A / Cross) = emergency stop
        if joystick.get_button(0):
            stop = True
        # Button 3 (Y / Triangle) = capture
        if joystick.get_button(3):
            with state.cmd_lock:
                state.command.capture = True

    # ── Keyboard (fallback / override) ────────────────────────────────
    if not joystick or (throttle == 0.0 and steering == 0.0 and not stop):
        if keys[pygame.K_w] or keys[pygame.K_UP]:
            throttle =  DRIVE_SPEED
        if keys[pygame.K_s] or keys[pygame.K_DOWN]:
            throttle = -DRIVE_SPEED
        if keys[pygame.K_a] or keys[pygame.K_LEFT]:
            steering = -TURN_SPEED
        if keys[pygame.K_d] or keys[pygame.K_RIGHT]:
            steering =  TURN_SPEED
        if keys[pygame.K_x]:
            stop = True

    with state.cmd_lock:
        state.command.throttle = throttle
        state.command.steering = steering
        state.command.stop     = stop

    return True


# ─────────────────────────────────────────────
# Rendering Helpers
# ─────────────────────────────────────────────

def draw_text(surface, text, font, colour, x, y):
    surf = font.render(text, True, colour)
    surface.blit(surf, (x, y))
    return surf.get_height()


def draw_bar(surface, x, y, w, h, value, vmin, vmax, colour):
    """Horizontal progress bar."""
    pygame.draw.rect(surface, C_BORDER, (x, y, w, h), border_radius=3)
    filled = int(w * max(0, min(1, (value - vmin) / (vmax - vmin))))
    if filled > 0:
        pygame.draw.rect(surface, colour, (x, y, filled, h), border_radius=3)


def format_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def battery_colour(v: float) -> tuple:
    if v >= 7.2:
        return C_ACCENT
    if v >= 6.6:
        return C_WARN
    return C_DANGER


# ─────────────────────────────────────────────
# Main Render Loop
# ─────────────────────────────────────────────

def render(screen, state: AppState, fonts: dict):
    screen.fill(C_BG)

    # ── Left panel: Video feed ─────────────────────────────────────────
    video_rect = pygame.Rect(0, 0, VIDEO_W, VIDEO_H)
    pygame.draw.rect(screen, C_VIDEO_BG, video_rect)

    with state.frame_lock:
        frame = state.latest_frame

    if frame is not None:
        # Scale to fit video area
        fh, fw = frame.shape[:2]
        scale  = min(VIDEO_W / fw, VIDEO_H / fh)
        nw, nh = int(fw * scale), int(fh * scale)
        resized = cv2.resize(frame, (nw, nh))
        surf    = pygame.surfarray.make_surface(resized.swapaxes(0, 1))
        ox = (VIDEO_W - nw) // 2
        oy = (VIDEO_H - nh) // 2
        screen.blit(surf, (ox, oy))
    else:
        # No signal placeholder
        pygame.draw.rect(screen, (20, 25, 35), video_rect)
        draw_text(screen, "NO VIDEO SIGNAL", fonts["mono_sm"],
                  C_SUBTEXT, VIDEO_W // 2 - 90, VIDEO_H // 2)

    # QR overlay text (from B2)
    if state.qr_overlay:
        label = fonts["mono_sm"].render(f"QR: {state.qr_overlay}", True, C_ACCENT)
        screen.blit(label, (10, VIDEO_H - 30))

    # ── Control direction indicator (below video) ──────────────────────
    ind_y   = VIDEO_H + 10
    ind_cx  = VIDEO_W // 2
    ind_cy  = ind_y + 40
    ind_r   = 30
    pygame.draw.circle(screen, C_BORDER, (ind_cx, ind_cy), ind_r, 1)
    dot_x = ind_cx + int(state.command.steering * ind_r * 0.9)
    dot_y = ind_cy - int(state.command.throttle * ind_r * 0.9)
    pygame.draw.circle(screen, C_ACCENT, (dot_x, dot_y), 6)
    draw_text(screen, "THROTTLE / STEERING", fonts["small"],
              C_SUBTEXT, ind_cx - 90, ind_y - 2)

    # ── Right panel ────────────────────────────────────────────────────
    px = VIDEO_W + 12
    py = 10
    pw = PANEL_W - 20

    # Title
    draw_text(screen, "ORT BASE STATION", fonts["title"], C_ACCENT, px, py)
    py += 36

    pygame.draw.line(screen, C_BORDER, (px, py), (px + pw, py))
    py += 10

    # Mission timer
    elapsed  = time.time() - state.mission_start
    remain   = MISSION_DURATION_S - elapsed
    timer_c  = C_DANGER if remain < 120 else (C_WARN if remain < 300 else C_TEXT)
    draw_text(screen, "MISSION TIME", fonts["small"], C_SUBTEXT, px, py)
    py += 16
    draw_text(screen, format_time(remain), fonts["large"], timer_c, px, py)
    py += 44

    pygame.draw.line(screen, C_BORDER, (px, py), (px + pw, py))
    py += 10

    # Link status
    t = state.telemetry
    link_c = C_ACCENT if t.link_ok else C_DANGER
    link_s = "LINK  ●  ONLINE" if t.link_ok else "LINK  ●  LOST"
    draw_text(screen, link_s, fonts["mono_sm"], link_c, px, py)
    py += 24

    # Battery
    draw_text(screen, "BATTERY", fonts["small"], C_SUBTEXT, px, py)
    py += 16
    batt_c = battery_colour(t.battery_v)
    draw_text(screen, f"{t.battery_v:.2f} V", fonts["mono_sm"], batt_c, px, py)
    draw_bar(screen, px, py + 20, pw, 8, t.battery_v, 6.0, 8.4, batt_c)
    py += 38

    # Temperature
    draw_text(screen, "CPU TEMP", fonts["small"], C_SUBTEXT, px, py)
    py += 16
    temp_c = C_DANGER if t.temp_c > 75 else C_WARN if t.temp_c > 60 else C_TEXT
    draw_text(screen, f"{t.temp_c:.1f} °C", fonts["mono_sm"], temp_c, px, py)
    py += 28

    # Uptime
    draw_text(screen, f"UPTIME  {format_time(t.uptime_s)}", fonts["small"],
              C_SUBTEXT, px, py)
    py += 28

    pygame.draw.line(screen, C_BORDER, (px, py), (px + pw, py))
    py += 10

    # Drive command readout
    draw_text(screen, "DRIVE COMMAND", fonts["small"], C_SUBTEXT, px, py)
    py += 16
    thr_str = f"THR  {state.command.throttle:+.2f}"
    str_str = f"STR  {state.command.steering:+.2f}"
    draw_text(screen, thr_str, fonts["mono_sm"], C_TEXT, px, py)
    py += 18
    draw_text(screen, str_str, fonts["mono_sm"], C_TEXT, px, py)
    py += 18
    if state.command.stop:
        draw_text(screen, "⚠  E-STOP ACTIVE", fonts["mono_sm"], C_DANGER, px, py)
    py += 22

    pygame.draw.line(screen, C_BORDER, (px, py), (px + pw, py))
    py += 10

    # Key bindings
    bindings = [
        ("W/↑",      "Forward"),
        ("S/↓",      "Reverse"),
        ("A/←",      "Turn Left"),
        ("D/→",      "Turn Right"),
        ("X",        "E-Stop"),
        ("SPACE",    "Capture QR"),
        ("ESC",      "Quit"),
    ]
    draw_text(screen, "CONTROLS", fonts["small"], C_SUBTEXT, px, py)
    py += 18
    for key, action in bindings:
        draw_text(screen, f"{key:<8} {action}", fonts["mono_sm"], C_TEXT, px, py)
        py += 16

    # Alerts (bottom of right panel)
    if state.alerts:
        py = WINDOW_H - len(state.alerts) * 18 - 10
        for alert in state.alerts[-4:]:
            draw_text(screen, f"⚠ {alert}", fonts["small"], C_WARN, px, py)
            py += 18

    pygame.display.flip()


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="B1 Operator Interface")
    parser.add_argument("--rover-ip", default=ROVER_IP_DEFAULT,
                        help="Rover IP address")
    parser.add_argument("--cmd-port",  type=int, default=CMD_PORT)
    parser.add_argument("--telem-port",type=int, default=TELEMETRY_PORT)
    parser.add_argument("--video-port",type=int, default=VIDEO_PORT)
    args = parser.parse_args()

    pygame.init()
    pygame.display.set_caption("ORT Base Station — B1 Operator Interface")
    screen = pygame.display.set_mode((WINDOW_W, WINDOW_H))
    clock  = pygame.time.Clock()

    # Fonts
    fonts = {
        "title":   pygame.font.SysFont("consolas",  18, bold=True),
        "large":   pygame.font.SysFont("consolas",  38, bold=True),
        "mono_sm": pygame.font.SysFont("consolas",  14),
        "small":   pygame.font.SysFont("consolas",  12),
    }

    # Joystick
    joystick = None
    if pygame.joystick.get_count() > 0:
        joystick = pygame.joystick.Joystick(0)
        joystick.init()
        print(f"[B1] Gamepad detected: {joystick.get_name()}")
    else:
        print("[B1] No gamepad found — keyboard only mode")

    state = AppState()

    # Start background threads
    threads = [
        threading.Thread(target=telemetry_receiver,
                         args=(state, args.telem_port), daemon=True),
        threading.Thread(target=video_receiver,
                         args=(state, args.video_port),  daemon=True),
        threading.Thread(target=command_sender,
                         args=(state, args.rover_ip, args.cmd_port), daemon=True),
    ]
    for t in threads:
        t.start()

    print(f"[B1] Running. Rover @ {args.rover_ip}")

    running = True
    while running:
        events = pygame.event.get()
        keys   = pygame.key.get_pressed()
        running = read_inputs(state, joystick, keys, events)
        render(screen, state, fonts)
        clock.tick(30)   # 30 FPS UI

    state.mission_active = False
    pygame.quit()
    print("[B1] Operator interface closed.")


if __name__ == "__main__":
    main()
