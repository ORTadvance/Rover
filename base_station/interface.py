import logging
import os
import sys
import threading
import time
from typing import Optional

import numpy as np
import pygame

import config
from config import (
    BATTERY_CRIT_V, BATTERY_WARN_V, TEMP_CRIT_C, TEMP_WARN_C,
    C_ACCENT, C_BG, C_BORDER, C_DANGER, C_PANEL, C_SUBTEXT, C_TAB_ACTIVE,
    C_TAB_INACTIVE, C_TEXT, C_VIDEO_BG, C_WARN,
    DRIVE_SPEED, SERVO_MAX_PW, SERVO_MIN_PW, SERVO_MID_PW, SERVO_STEP_PW,
    TARGET_FPS, TURN_SPEED, VIDEO_H, VIDEO_W, WINDOW_H, WINDOW_W,
)
from shared_state import AppState

log = logging.getLogger("interface")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clr_battery(v: float):
    if v >= BATTERY_WARN_V:
        return C_ACCENT
    if v >= BATTERY_CRIT_V:
        return C_WARN
    return C_DANGER


def _clr_temp(t: float):
    if t < TEMP_WARN_C:
        return C_ACCENT
    if t < TEMP_CRIT_C:
        return C_WARN
    return C_DANGER


def _clr_state(s: str):
    if s == "OPERATING":
        return C_ACCENT
    if s == "IDLE":
        return C_WARN
    return C_DANGER


def _fmt_uptime(s: float) -> str:
    m = int(s) // 60
    sec = int(s) % 60
    return f"{m:02d}:{sec:02d}"


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


# ── Font cache ────────────────────────────────────────────────────────────────

_font_cache: dict = {}


def _font(size: int, mono: bool = False) -> pygame.font.Font:
    key = (size, mono)
    if key not in _font_cache:
        if mono:
            f = pygame.font.SysFont("consolas,monospace", size)
        else:
            f = pygame.font.SysFont("segoeui,arial,sans", size)
        _font_cache[key] = f
    return _font_cache[key]


def _text(surf: pygame.Surface, txt: str, pos, size: int = 14,
          colour=C_TEXT, mono: bool = False, anchor: str = "topleft"):
    rendered = _font(size, mono).render(str(txt), True, colour)
    r = rendered.get_rect(**{anchor: pos})
    surf.blit(rendered, r)
    return r


def _hbar(surf: pygame.Surface, rect: pygame.Rect, frac: float, colour=C_ACCENT, bg=C_BORDER):
    pygame.draw.rect(surf, bg, rect, border_radius=2)
    filled = rect.copy()
    filled.width = max(0, int(rect.width * min(max(frac, 0.0), 1.0)))
    if filled.width > 0:
        pygame.draw.rect(surf, colour, filled, border_radius=2)


def _vbar(surf: pygame.Surface, rect: pygame.Rect, frac: float, colour=C_ACCENT, bg=C_BORDER):
    """Vertical bar — frac=1 means top."""
    pygame.draw.rect(surf, bg, rect, border_radius=2)
    filled = rect.copy()
    h = max(0, int(rect.height * min(max(frac, 0.0), 1.0)))
    filled.height = h
    filled.top = rect.bottom - h
    if filled.height > 0:
        pygame.draw.rect(surf, colour, filled, border_radius=2)


def _panel(surf: pygame.Surface, rect: pygame.Rect):
    pygame.draw.rect(surf, C_PANEL, rect, border_radius=4)
    pygame.draw.rect(surf, C_BORDER, rect, width=1, border_radius=4)


def _button(surf: pygame.Surface, rect: pygame.Rect, label: str,
            active: bool = True, size: int = 13) -> pygame.Rect:
    colour = C_ACCENT if active else C_BORDER
    pygame.draw.rect(surf, colour, rect, border_radius=3)
    pygame.draw.rect(surf, C_BORDER, rect, width=1, border_radius=3)
    lbl_col = C_BG if active else C_SUBTEXT
    _text(surf, label, rect.center, size=size, colour=lbl_col, anchor="center")
    return rect


# ── Panel renderers ──────────────────────────────────────────────────────────

def _draw_video_panel(surf: pygame.Surface, state: AppState, panel_rect: pygame.Rect,
                      mock_mode: bool):
    pygame.draw.rect(surf, C_VIDEO_BG, panel_rect)

    with state.frame_lock:
        frame = state.latest_frame.copy() if state.latest_frame is not None else None

    if frame is None:
        _text(surf, "NO VIDEO SIGNAL", panel_rect.center, size=20,
              colour=C_SUBTEXT, anchor="center")
    else:
        # BGR → RGB
        rgb = frame[:, :, ::-1]
        try:
            py_surf = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
            scaled = pygame.transform.scale(py_surf, (panel_rect.width, panel_rect.height))
            surf.blit(scaled, panel_rect.topleft)
        except Exception:
            _text(surf, "VIDEO ERROR", panel_rect.center, size=16,
                  colour=C_DANGER, anchor="center")

    if mock_mode:
        banner_rect = pygame.Rect(panel_rect.x, panel_rect.y, panel_rect.width, 28)
        s = pygame.Surface((banner_rect.width, banner_rect.height), pygame.SRCALPHA)
        s.fill((255, 160, 0, 160))
        surf.blit(s, banner_rect.topleft)
        _text(surf, "[ SIMULATION — NO REAL ROVER ]",
              (banner_rect.centerx, banner_rect.centery),
              size=13, colour=(0, 0, 0), anchor="center")

    # QR overlay text at bottom of video panel
    with state.state_lock:
        qr_overlay = state.qr_overlay

    if qr_overlay:
        label = _truncate(f"QR: {qr_overlay}", 50)
        qr_rect = pygame.Rect(panel_rect.x + 6, panel_rect.bottom - 26, panel_rect.width - 12, 22)
        s2 = pygame.Surface((qr_rect.width, qr_rect.height), pygame.SRCALPHA)
        s2.fill((0, 0, 0, 160))
        surf.blit(s2, qr_rect.topleft)
        _text(surf, label, (qr_rect.x + 6, qr_rect.y + 4), size=12, colour=C_ACCENT, mono=True)


def _draw_ir_servo(surf: pygame.Surface, state: AppState, x: int, y: int) -> int:
    with state.state_lock:
        ir_f = state.telemetry.ir_front
        ir_r = state.telemetry.ir_rear
        pw   = state.telemetry.servo_pw

    # IR dots
    _text(surf, "IR:", (x, y), size=13, colour=C_SUBTEXT)
    fx = x + 30
    pygame.draw.circle(surf, C_DANGER if ir_f else C_ACCENT, (fx, y + 6), 7)
    _text(surf, "F", (fx, y), size=10, colour=C_BG, anchor="center")
    rx = fx + 22
    pygame.draw.circle(surf, C_DANGER if ir_r else C_ACCENT, (rx, y + 6), 7)
    _text(surf, "R", (rx, y), size=10, colour=C_BG, anchor="center")
    y += 22

    # Servo bar
    _text(surf, "SERVO:", (x, y), size=13, colour=C_SUBTEXT)
    bar_rect = pygame.Rect(x + 55, y + 2, 80, 10)
    frac = (pw - SERVO_MIN_PW) / max(1, SERVO_MAX_PW - SERVO_MIN_PW)
    _hbar(surf, bar_rect, frac, colour=C_ACCENT)
    _text(surf, str(pw), (bar_rect.right + 6, y), size=12, colour=C_TEXT, mono=True)
    return y + 20


def _draw_telemetry_panel(surf: pygame.Surface, state: AppState,
                          panel_rect: pygame.Rect) -> None:
    _panel(surf, panel_rect)
    x = panel_rect.x + 10
    y = panel_rect.y + 8

    # Title
    _text(surf, "ORT BASE STATION", (x, y), size=14, colour=C_ACCENT)
    y += 22
    pygame.draw.line(surf, C_BORDER, (x, y), (panel_rect.right - 10, y))
    y += 8

    # Link status
    with state.state_lock:
        link_ok     = state.telemetry.link_ok
        rover_state = state.telemetry.rover_state
        battery_v   = state.telemetry.battery_v
        temp_c      = state.telemetry.temp_c
        uptime_s    = state.telemetry.uptime_s
        lat_ms      = state.link.cmd_latency_ms
    with state.cmd_lock:
        throttle = state.command.throttle
        steering = state.command.steering
        e_stop   = state.command.e_stop

    link_txt   = "ONLINE" if link_ok else "LOST"
    link_col   = C_ACCENT if link_ok else C_DANGER
    _text(surf, f"LINK  ● {link_txt}", (x, y), size=13, colour=link_col, mono=True)
    y += 18

    state_col = _clr_state(rover_state)
    _text(surf, f"ROVER: {rover_state}", (x, y), size=13, colour=state_col, mono=True)
    y += 18

    pygame.draw.line(surf, C_BORDER, (x, y), (panel_rect.right - 10, y))
    y += 8

    # Battery
    bat_col = _clr_battery(battery_v)
    _text(surf, f"BATTERY  {battery_v:.2f} V", (x, y), size=13, colour=bat_col, mono=True)
    y += 16
    bat_frac = max(0.0, (battery_v - 6.0) / (8.4 - 6.0))
    _hbar(surf, pygame.Rect(x, y, panel_rect.width - 20, 7), bat_frac, colour=bat_col)
    y += 14

    # CPU temp
    tmp_col = _clr_temp(temp_c)
    _text(surf, f"CPU TEMP  {temp_c:.1f} C", (x, y), size=13, colour=tmp_col, mono=True)
    y += 16

    # Uptime
    _text(surf, f"UPTIME  {_fmt_uptime(uptime_s)}", (x, y), size=13, colour=C_TEXT, mono=True)
    y += 18

    pygame.draw.line(surf, C_BORDER, (x, y), (panel_rect.right - 10, y))
    y += 8

    # Latency
    _text(surf, f"CMD LAT  {lat_ms:.0f} ms", (x, y), size=13, colour=C_TEXT, mono=True)
    y += 16
    _text(surf, f"THR  {throttle:+.2f}", (x, y), size=13, colour=C_TEXT, mono=True)
    y += 16
    _text(surf, f"STR  {steering:+.2f}", (x, y), size=13, colour=C_TEXT, mono=True)
    y += 16
    if e_stop:
        _text(surf, "!! E-STOP ACTIVE !!", (x, y), size=13, colour=C_DANGER, mono=True)
    y += 18

    pygame.draw.line(surf, C_BORDER, (x, y), (panel_rect.right - 10, y))
    y += 8

    # Controls reference
    controls = [
        ("W/↑", "Forward"),
        ("S/↓", "Reverse"),
        ("A/←", "Left"),
        ("D/→", "Right"),
        ("Q",   "Tilt Up"),
        ("E",   "Tilt Down"),
        ("X",   "E-Stop"),
        ("R",   "Release"),
        ("SPC", "Capture"),
    ]
    _text(surf, "CONTROLS:", (x, y), size=12, colour=C_SUBTEXT)
    y += 16
    for key, desc in controls:
        _text(surf, key, (x, y), size=11, colour=C_ACCENT, mono=True)
        _text(surf, desc, (x + 42, y), size=11, colour=C_SUBTEXT)
        y += 14


def _draw_alerts(surf: pygame.Surface, state: AppState, rect: pygame.Rect):
    with state.state_lock:
        alerts = list(state.alerts[-4:])

    if not alerts:
        return

    y = rect.bottom - (len(alerts) * 18 + 6)
    for msg in reversed(alerts):
        col = C_DANGER if any(w in msg for w in ("CRITICAL", "SAFE", "ERROR", "LOST")) else C_WARN
        _text(surf, f"⚠ {msg}", (rect.x + 6, y), size=12, colour=col)
        y += 18


def _draw_obstacle_toggle(surf: pygame.Surface, state: AppState, rect: pygame.Rect) -> pygame.Rect:
    enabled = state.obstacle_block_enabled
    label = "OBSTACLE BLOCK: ON" if enabled else "OBSTACLE BLOCK: OFF"
    colour = C_ACCENT if enabled else C_BORDER
    pygame.draw.rect(surf, colour, rect, border_radius=3)
    pygame.draw.rect(surf, C_BORDER, rect, width=1, border_radius=3)
    lbl_col = C_BG if enabled else C_SUBTEXT
    _text(surf, label, rect.center, size=12, colour=lbl_col, anchor="center")
    return rect


def _draw_tab_buttons(surf: pygame.Surface, active_tab: int,
                      rect_drive: pygame.Rect, rect_qr: pygame.Rect):
    for i, (r, label) in enumerate([(rect_drive, "DRIVE"), (rect_qr, "QR LOG")]):
        col = C_TAB_ACTIVE if i == active_tab else C_TAB_INACTIVE
        pygame.draw.rect(surf, col, r, border_radius=3)
        pygame.draw.rect(surf, C_BORDER, r, width=1, border_radius=3)
        txt_col = C_BG if i == active_tab else C_SUBTEXT
        _text(surf, label, r.center, size=13, colour=txt_col, anchor="center")


def _load_thumbnail(image_path: str) -> Optional[pygame.Surface]:
    if not image_path or not os.path.isfile(image_path):
        return None
    try:
        surf = pygame.image.load(image_path)
        return pygame.transform.scale(surf, (80, 60))
    except Exception:
        return None


def _draw_qr_tab(surf: pygame.Surface, state: AppState, panel_rect: pygame.Rect,
                 thumb_cache: dict):
    _panel(surf, panel_rect)
    x = panel_rect.x + 10
    y = panel_rect.y + 10

    with state.state_lock:
        qr_history = list(state.qr_history)
    captures = len(qr_history)
    unique = len({e["text"] for e in qr_history})

    _text(surf, f"QR LOG   Captures: {captures}   Unique: {unique}",
          (x, y), size=15, colour=C_ACCENT)
    y += 26

    pygame.draw.line(surf, C_BORDER, (x, y), (panel_rect.right - 10, y))
    y += 8

    # Header row
    hdr_cols = [(x, "#"), (x + 28, "Time"), (x + 90, "QR Text"), (x + 420, "Image")]
    for hx, hl in hdr_cols:
        _text(surf, hl, (hx, y), size=12, colour=C_SUBTEXT)
    y += 16
    pygame.draw.line(surf, C_BORDER, (x, y), (panel_rect.right - 10, y))
    y += 4

    # Rows — show last N that fit
    row_h = 66
    max_rows = max(1, (panel_rect.bottom - y - 10) // row_h)
    visible = qr_history[-max_rows:]
    start_idx = len(qr_history) - len(visible)

    for i, entry in enumerate(visible):
        idx = start_idx + i + 1
        ts  = entry.get("timestamp_str", "")[-8:]  # HH:MM:SS
        txt = _truncate(entry.get("text", ""), 40)
        img = entry.get("image_path", "")

        row_rect = pygame.Rect(x, y, panel_rect.width - 20, row_h - 4)
        pygame.draw.rect(surf, C_BORDER, row_rect, width=1, border_radius=2)

        _text(surf, str(idx), (x + 4, y + 22), size=12, colour=C_TEXT, mono=True)
        _text(surf, ts, (x + 28, y + 22), size=12, colour=C_TEXT, mono=True)
        _text(surf, txt, (x + 90, y + 22), size=12, colour=C_ACCENT, mono=True)

        # Thumbnail
        if img not in thumb_cache:
            thumb_cache[img] = _load_thumbnail(img)
        thumb = thumb_cache[img]
        if thumb:
            surf.blit(thumb, (x + 420, y + 2))
        else:
            placeholder = pygame.Rect(x + 420, y + 2, 80, 60)
            pygame.draw.rect(surf, C_BORDER, placeholder)
            _text(surf, "N/A", (placeholder.centerx, placeholder.centery),
                  size=10, colour=C_SUBTEXT, anchor="center")

        y += row_h


# ── Main run function ─────────────────────────────────────────────────────────

def run(
    state: AppState,
    b3_logger,
    mock_mode: bool = False,
    fullscreen: bool = False,
    no_gamepad: bool = False,
) -> None:
    """Blocking. Returns when operator quits (ESC or window close)."""

    pygame.init()
    pygame.display.set_caption("ORT Base Station")

    flags = pygame.DOUBLEBUF | (pygame.FULLSCREEN if fullscreen else 0)
    screen = pygame.display.set_mode((WINDOW_W, WINDOW_H), flags)
    clock  = pygame.time.Clock()

    # Gamepad init
    joystick: Optional[pygame.joystick.Joystick] = None
    if not no_gamepad:
        pygame.joystick.init()
        if pygame.joystick.get_count() > 0:
            joystick = pygame.joystick.Joystick(0)
            joystick.init()
            log.info("Gamepad detected: %s", joystick.get_name())

    # Shared UI state
    active_tab    = 0           # 0 = DRIVE, 1 = QR LOG
    thumb_cache: dict = {}
    export_msg: Optional[str]   = None
    export_msg_until: float     = 0.0
    export_disabled_until: float = 0.0
    _export_result: list = []   # populated by background export thread
    _last_rover_alert: Optional[str] = None  # tracks which rover-state alert is active

    # ── Layout constants ──────────────────────────────────────────────────────
    SIDE_W   = WINDOW_W - VIDEO_W          # right panel width
    PANEL_X  = VIDEO_W                     # right panel left edge
    TAB_H    = 28
    BTN_H    = 28
    BTN_W    = 110

    video_rect   = pygame.Rect(0, 0, VIDEO_W, VIDEO_H)
    right_rect   = pygame.Rect(PANEL_X, 0, SIDE_W, WINDOW_H)

    # Tab buttons (bottom of left column)
    tab_y        = WINDOW_H - BTN_H - 6
    tab_drive_r  = pygame.Rect(6,      tab_y, 100, BTN_H)
    tab_qr_r     = pygame.Rect(112,    tab_y, 100, BTN_H)

    # Obstacle toggle (Tab 1 only)
    obs_btn_rect = pygame.Rect(6, VIDEO_H + 4, 200, BTN_H)

    # IR / Servo strip (below video, Tab 1)
    ir_y = VIDEO_H + 38

    # Export button
    export_rect  = pygame.Rect(PANEL_X + SIDE_W - BTN_W - 8,
                               WINDOW_H - BTN_H - 6, BTN_W, BTN_H)

    # QR tab panel fills the whole left side
    qr_panel_rect = pygame.Rect(0, 0, VIDEO_W, WINDOW_H - BTN_H - 10)

    running = True
    while running:
        dt = clock.tick(TARGET_FPS) / 1000.0

        # ── Key state ──────────────────────────────────────────────────────
        keys = pygame.key.get_pressed()
        throttle = 0.0
        steering = 0.0

        if keys[pygame.K_w] or keys[pygame.K_UP]:
            throttle += DRIVE_SPEED
        if keys[pygame.K_s] or keys[pygame.K_DOWN]:
            throttle -= DRIVE_SPEED
        if keys[pygame.K_a] or keys[pygame.K_LEFT]:
            steering -= TURN_SPEED
        if keys[pygame.K_d] or keys[pygame.K_RIGHT]:
            steering += TURN_SPEED

        # Gamepad axes
        if joystick:
            ax_y = joystick.get_axis(1)  # left stick Y (usually inverted)
            ax_x = joystick.get_axis(0)  # left stick X
            dz = config.GAMEPAD_DEADZONE
            if abs(ax_y) > dz:
                throttle += -ax_y * DRIVE_SPEED
            if abs(ax_x) > dz:
                steering += ax_x * TURN_SPEED

        with state.cmd_lock:
            if not state.command.e_stop:
                state.command.throttle = max(-1.0, min(1.0, throttle))
                state.command.steering = max(-1.0, min(1.0, steering))
            else:
                state.command.throttle = 0.0
                state.command.steering = 0.0

        # ── Events ────────────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False

                elif event.key == pygame.K_TAB:
                    active_tab = 1 - active_tab

                elif event.key == pygame.K_x:
                    with state.cmd_lock:
                        state.command.e_stop = True
                    state.add_alert("E-STOP ACTIVE")
                    b3_logger.log_action("ESTOP")

                elif event.key == pygame.K_r:
                    with state.cmd_lock:
                        state.command.e_stop = False
                    state.clear_alert("E-STOP ACTIVE")

                elif event.key == pygame.K_q:
                    with state.cmd_lock:
                        state.command.servo_delta = SERVO_STEP_PW

                elif event.key == pygame.K_e:
                    with state.cmd_lock:
                        state.command.servo_delta = -SERVO_STEP_PW

                elif event.key == pygame.K_SPACE:
                    with state.cmd_lock:
                        state.command.capture = True

            elif event.type == pygame.JOYBUTTONDOWN:
                if event.button == 0:   # A/Cross — E-Stop
                    with state.cmd_lock:
                        state.command.e_stop = True
                    state.add_alert("E-STOP ACTIVE")
                    b3_logger.log_action("ESTOP")
                elif event.button == 1:  # B/Circle — Release E-Stop
                    with state.cmd_lock:
                        state.command.e_stop = False
                    state.clear_alert("E-STOP ACTIVE")
                elif event.button == 3:  # Y/Triangle — Capture
                    with state.cmd_lock:
                        state.command.capture = True
                elif event.button == 4:  # L1 — servo tilt down
                    with state.cmd_lock:
                        state.command.servo_delta = -SERVO_STEP_PW
                elif event.button == 5:  # R1 — servo tilt up
                    with state.cmd_lock:
                        state.command.servo_delta = SERVO_STEP_PW

            elif event.type == pygame.JOYHATMOTION:
                pass  # hat events not used; shoulder buttons handled above

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos

                # Tab buttons
                if tab_drive_r.collidepoint(mx, my):
                    active_tab = 0
                elif tab_qr_r.collidepoint(mx, my):
                    active_tab = 1

                # Obstacle toggle (Tab 1 only)
                elif active_tab == 0 and obs_btn_rect.collidepoint(mx, my):
                    state.obstacle_block_enabled = not state.obstacle_block_enabled

                # Export button — run in background so the UI thread stays responsive
                elif export_rect.collidepoint(mx, my):
                    now = time.monotonic()
                    if now >= export_disabled_until:
                        export_disabled_until = now + 60.0  # block button while running
                        _export_result.clear()

                        def _do_export(result_list, logger):
                            try:
                                path = logger.export()
                                result_list.append(f"Saved to: {path}")
                            except Exception as exc:
                                result_list.append(f"Export error: {exc}")

                        threading.Thread(
                            target=_do_export,
                            args=(_export_result, b3_logger),
                            daemon=True,
                        ).start()

        # ── Alert triggers from telemetry ──────────────────────────────────
        with state.state_lock:
            bv  = state.telemetry.battery_v
            tc  = state.telemetry.temp_c
            rst = state.telemetry.rover_state

        if 0.0 < bv < BATTERY_CRIT_V:
            state.add_alert("BATTERY CRITICAL")
            state.clear_alert("BATTERY LOW")
        elif 0.0 < bv < BATTERY_WARN_V:
            state.add_alert("BATTERY LOW")
            state.clear_alert("BATTERY CRITICAL")
        else:
            state.clear_alert("BATTERY CRITICAL")
            state.clear_alert("BATTERY LOW")

        if tc >= TEMP_CRIT_C:
            state.add_alert("CPU HOT")
        else:
            state.clear_alert("CPU HOT")

        if rst in ("SAFE_STATE", "ERROR"):
            new_rover_alert = f"ROVER {rst}"
            if new_rover_alert != _last_rover_alert:
                if _last_rover_alert:
                    state.clear_alert(_last_rover_alert)
                state.add_alert(new_rover_alert)
                _last_rover_alert = new_rover_alert
        else:
            if _last_rover_alert:
                state.clear_alert(_last_rover_alert)
                _last_rover_alert = None

        # ── Draw ──────────────────────────────────────────────────────────
        screen.fill(C_BG)

        if active_tab == 0:
            # ── Tab 1: DRIVE ──────────────────────────────────────────────
            _draw_video_panel(screen, state, video_rect, mock_mode)

            # IR / servo / obstacle strip below video
            bottom_strip = pygame.Rect(0, VIDEO_H, VIDEO_W, WINDOW_H - VIDEO_H - BTN_H - 10)
            pygame.draw.rect(screen, C_PANEL, bottom_strip)
            pygame.draw.line(screen, C_BORDER, (0, VIDEO_H), (VIDEO_W, VIDEO_H))

            strip_y = VIDEO_H + 6
            _draw_ir_servo(screen, state, 6, strip_y)
            _draw_obstacle_toggle(screen, state, obs_btn_rect)

        else:
            # ── Tab 2: QR LOG ────────────────────────────────────────────
            _draw_qr_tab(screen, state, qr_panel_rect, thumb_cache)

        # Right telemetry panel (both tabs)
        _draw_telemetry_panel(screen, state, right_rect)
        _draw_alerts(screen, state, right_rect)

        # Tab buttons
        _draw_tab_buttons(screen, active_tab, tab_drive_r, tab_qr_r)

        # Collect result from background export thread when it finishes
        if _export_result:
            export_msg = _export_result[0]
            _export_result.clear()
            export_msg_until      = time.monotonic() + 3.0
            export_disabled_until = export_msg_until

        # Export button
        now_m = time.monotonic()
        if export_msg and now_m < export_msg_until:
            exp_label = "EXPORTED ✓"
            exp_active = False
        else:
            exp_label = "EXPORT"
            exp_active = True
            export_msg = None
        _button(screen, export_rect, exp_label, active=exp_active, size=13)

        # Export confirmation tooltip
        if export_msg and now_m < export_msg_until:
            tip = _truncate(export_msg, 60)
            _text(screen, tip,
                  (export_rect.right - 4, export_rect.y - 18),
                  size=11, colour=C_ACCENT, mono=True, anchor="topright")

        pygame.display.flip()

    pygame.quit()
    log.info("Interface closed.")


# ── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))

    logging.basicConfig(level=logging.DEBUG)

    import threading

    class _MockLogger:
        def export(self):
            return "missions/test/submission_package.json"
        def log_action(self, action, detail=""):
            print(f"[LOG] {action}: {detail}")

    state = AppState()
    state.telemetry.link_ok      = True
    state.telemetry.rover_state  = "OPERATING"
    state.telemetry.battery_v    = 7.8
    state.telemetry.temp_c       = 52.0
    state.telemetry.uptime_s     = 135.0
    state.telemetry.ir_front     = False
    state.telemetry.ir_rear      = True
    state.telemetry.servo_pw     = SERVO_MID_PW
    state.link.cmd_latency_ms    = 42.0

    state.add_qr("SITE_ALPHA_42", "", "2026-01-01 12:03:15")
    state.add_qr("SITE_BETA_07",  "", "2026-01-01 12:07:44")
    state.add_alert("BATTERY LOW")

    # Simulate a live frame
    def _frame_loop():
        import numpy as np
        while True:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            frame[:, :, 1] = 40   # dark green tint
            import cv2
            cv2.putText(frame, "MOCK VIDEO", (180, 240),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 200, 120), 2)
            with state.frame_lock:
                state.latest_frame = frame
            time.sleep(1.0 / 30)

    t = threading.Thread(target=_frame_loop, daemon=True)
    t.start()

    run(state, _MockLogger(), mock_mode=True)
