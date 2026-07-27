#!/usr/bin/env python3
"""
tests/mock_rover.py — Simulated rover for integration testing.

Binds ZMQ sockets on the same ports as the real rover, pushes synthetic
telemetry + video, and prints received commands to stdout.

Usage:
    python tests/mock_rover.py                    # normal run
    python tests/mock_rover.py --link-drop-at 60  # drop link at t=60s for 5s
"""
import argparse
import json
import struct
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import zmq

# Port constants — keep in sync with rover/config.py.
CMD_SUB_PORT    = 5556
TELEM_PUSH_PORT = 5557
VIDEO_PUSH_PORT = 5558


def _make_frame(frame_idx: int, battery_v: float, uptime: float) -> bytes:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = (20, 30, 50)
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    cv2.putText(frame, f"MOCK ROVER  {ts}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 120), 2)
    cv2.putText(frame, f"batt={battery_v:.2f}V  up={uptime:.0f}s",
                (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (150, 200, 150), 1)

    # Embed a real QR code every 30 frames, cycling through 3 sites
    if frame_idx % 30 == 0:
        try:
            import qrcode
            from PIL import Image
            sites = ["MOCK_SITE_01", "MOCK_SITE_02", "MOCK_SITE_03"]
            site = sites[(frame_idx // 30) % len(sites)]
            qr = qrcode.QRCode(box_size=3, border=2)
            qr.add_data(site)
            qr.make(fit=True)
            pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            overlay = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
            x = 640 // 2 - overlay.shape[1] // 2
            y = 480 // 2 - overlay.shape[0] // 2
            x2 = min(x + overlay.shape[1], 640)
            y2 = min(y + overlay.shape[0], 480)
            frame[y:y2, x:x2] = overlay[:y2 - y, :x2 - x]
            cv2.putText(frame, site, (10, 460),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 200), 1)
        except ImportError:
            pass  # qrcode not available — frames without QR are fine

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if ok:
        jpeg = buf.tobytes()
        return struct.pack(">I", len(jpeg)) + jpeg
    return b""


def main():
    parser = argparse.ArgumentParser(description="Mock rover for ORT integration testing")
    parser.add_argument(
        "--link-drop-at", type=int, default=None, metavar="SECONDS",
        help="Stop sending telemetry for 5 s starting at T seconds elapsed",
    )
    args = parser.parse_args()

    ctx = zmq.Context()

    # Rover BINDs on all three ports; base station CONNECTs.
    telem_pub = ctx.socket(zmq.PUB)
    telem_pub.setsockopt(zmq.LINGER, 0)
    telem_pub.setsockopt(zmq.SNDHWM, 2)
    telem_pub.bind(f"tcp://*:{TELEM_PUSH_PORT}")

    video_pub = ctx.socket(zmq.PUB)
    video_pub.setsockopt(zmq.LINGER, 0)
    video_pub.setsockopt(zmq.SNDHWM, 2)
    video_pub.bind(f"tcp://*:{VIDEO_PUSH_PORT}")

    cmd_sub = ctx.socket(zmq.SUB)
    cmd_sub.setsockopt(zmq.LINGER, 0)
    cmd_sub.setsockopt_string(zmq.SUBSCRIBE, "")
    cmd_sub.bind(f"tcp://*:{CMD_SUB_PORT}")

    print(f"Mock rover bound — cmd:{CMD_SUB_PORT}  telem:{TELEM_PUSH_PORT}  video:{VIDEO_PUSH_PORT}")
    if args.link_drop_at:
        print(f"  Link drop scheduled: t={args.link_drop_at}s → silence for 5 s")
    print("  Ctrl-C to stop\n")

    t_start     = time.monotonic()
    frame_idx   = 0
    # Battery drains 8.4 V → 6.0 V over ~10 min (600 s)
    batt_start  = 8.4
    batt_end    = 6.0

    try:
        while True:
            t0      = time.monotonic()
            elapsed = t0 - t_start
            battery = max(batt_end, batt_start - (elapsed / 600.0) * (batt_start - batt_end))
            temp_c  = 45.0 + min(25.0, elapsed * 0.04)

            # Simulated link drop window
            link_drop = (args.link_drop_at is not None
                         and args.link_drop_at <= elapsed < args.link_drop_at + 5)

            if not link_drop:
                telem = {
                    "battery_v":     round(battery, 3),
                    "temp_c":        round(temp_c, 1),
                    "uptime_s":      round(elapsed, 1),
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
                    telem_pub.send(json.dumps(telem).encode(), zmq.NOBLOCK)
                except zmq.ZMQError:
                    pass
            else:
                print(f"  [LINK DROP] t={elapsed:.1f}s — telemetry suppressed")

            # Video ~30 FPS
            packet = _make_frame(frame_idx, battery, elapsed)
            if packet:
                try:
                    video_pub.send(packet, zmq.NOBLOCK)
                except zmq.ZMQError:
                    pass
            frame_idx += 1

            # Drain incoming commands and print them
            while True:
                try:
                    raw = cmd_sub.recv(zmq.NOBLOCK)
                    cmd = json.loads(raw.decode())
                    print(f"  [CMD] {cmd}")
                except zmq.Again:
                    break
                except Exception as exc:
                    print(f"  [CMD parse error] {exc}")

            elapsed_loop = time.monotonic() - t0
            time.sleep(max(0.0, (1.0 / 30) - elapsed_loop))

    except KeyboardInterrupt:
        print("\nMock rover stopped.")
    finally:
        for sock in (telem_pub, video_pub, cmd_sub):
            sock.close()
        ctx.term()


if __name__ == "__main__":
    main()
