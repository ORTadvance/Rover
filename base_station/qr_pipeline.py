import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import cv2
import numpy as np
from pyzbar import pyzbar

from config import (
    QR_AUTO_CAPTURE_COOLDOWN_S,
    QR_PROCESS_FPS,
    QR_QUALITY_THRESHOLD,
)

log = logging.getLogger("qr_pipeline")


def _score_detection(det, frame_h: int, frame_w: int) -> float:
    """Score a pyzbar detection 0.0–1.0."""
    pts = np.array([(p.x, p.y) for p in det.polygon], dtype=np.float32)

    x, y, bw, bh = cv2.boundingRect(pts.astype(np.int32))

    frame_area = frame_h * frame_w
    qr_area = bw * bh
    size_score = min(qr_area / (0.05 * frame_area), 1.0) if frame_area > 0 else 0.0

    if max(bw, bh) > 0:
        aspect_score = min(bw, bh) / max(bw, bh)
    else:
        aspect_score = 0.0

    if len(pts) >= 3:
        poly_pts = pts.astype(np.int32)
        hull = cv2.convexHull(poly_pts)
        poly_area = cv2.contourArea(poly_pts)
        hull_area = cv2.contourArea(hull)
        poly_score = min(poly_area / hull_area, 1.0) if hull_area > 0 else 0.0
    else:
        poly_score = 0.0

    return 0.5 * size_score + 0.25 * aspect_score + 0.25 * poly_score


def _annotate_frame(
    frame: np.ndarray,
    scored_detections: list,
    threshold: float,
) -> np.ndarray:
    """Draw QR overlays and crosshair onto frame in-place."""
    fh, fw = frame.shape[:2]
    cx, cy = fw // 2, fh // 2

    # Crosshair guide
    cv2.line(frame, (cx - 20, cy), (cx + 20, cy), (100, 100, 100), 1)
    cv2.line(frame, (cx, cy - 20), (cx, cy + 20), (100, 100, 100), 1)

    for det, score in scored_detections:
        pts = np.array([(p.x, p.y) for p in det.polygon], dtype=np.int32)
        good = score >= threshold
        # BGR: green for good, amber for below threshold
        colour = (0, 200, 100) if good else (30, 180, 255)

        cv2.polylines(frame, [pts.reshape(-1, 1, 2)], True, colour, 2)

        bx, by, bw, bh = cv2.boundingRect(pts)

        raw_text = det.data.decode("utf-8", errors="replace")
        label = raw_text[:30]

        label_y = max(by - 22, 12)
        cv2.putText(frame, label, (bx, label_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)

        q_label = f"Q:{score:.2f}"
        cv2.putText(frame, q_label, (bx, label_y + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)

        # Quality bar at bottom of bounding box
        bar_y = by + bh + 4
        cv2.rectangle(frame, (bx, bar_y), (bx + bw, bar_y + 6), (50, 50, 50), -1)
        fill_w = int(bw * min(score, 1.0))
        cv2.rectangle(frame, (bx, bar_y), (bx + fill_w, bar_y + 6), colour, -1)

        # Directional arrow toward QR centre when below threshold
        if not good:
            qr_cx = bx + bw // 2
            qr_cy = by + bh // 2
            dx = qr_cx - cx
            dy = qr_cy - cy
            dist = max(1.0, (dx ** 2 + dy ** 2) ** 0.5)
            nx, ny = dx / dist, dy / dist
            tip_x = int(cx + nx * 40)
            tip_y = int(cy + ny * 40)
            cv2.arrowedLine(frame, (cx, cy), (tip_x, tip_y),
                            (30, 180, 255), 2, tipLength=0.3)

    return frame


def _save_still(frame: np.ndarray, save_dir: str, suffix: str) -> str:
    """Save frame as JPEG to save_dir. Returns the saved file path."""
    os.makedirs(save_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"still_{ts}_{suffix}.jpg"
    path = os.path.join(save_dir, filename)
    cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return path


class QRPipeline:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._state = None
        self._b3_log_fn: Optional[Callable] = None
        self._save_dir: str = ""
        self._last_auto_capture: float = 0.0

    def start(self, state, b3_log_fn: Callable, save_dir: str) -> None:
        """Start the background QR processing thread."""
        self._state = state
        self._b3_log_fn = b3_log_fn
        self._save_dir = save_dir
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="qr_pipeline")
        self._thread.start()
        log.info("QRPipeline started (%.0f fps, threshold=%.2f)",
                 QR_PROCESS_FPS, QR_QUALITY_THRESHOLD)

    def stop(self) -> None:
        """Signal the pipeline thread to stop and wait for it."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        log.info("QRPipeline stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        interval = 1.0 / QR_PROCESS_FPS
        state = self._state

        while self._running:
            t0 = time.monotonic()

            # Grab current frame
            with state.frame_lock:
                frame = state.latest_frame
                if frame is not None:
                    frame = frame.copy()

            if frame is None:
                time.sleep(interval)
                continue

            fh, fw = frame.shape[:2]

            # Detect QR codes
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            grey_eq = cv2.equalizeHist(grey)
            raw_dets = pyzbar.decode(grey_eq)

            # Score each detection
            scored = [(det, _score_detection(det, fh, fw)) for det in raw_dets]

            # Keep a clean copy for saving stills before annotation draws on frame
            clean_frame = frame.copy()

            # Annotate frame in-place then write back for display
            _annotate_frame(frame, scored, QR_QUALITY_THRESHOLD)
            with state.frame_lock:
                state.latest_frame = frame

            # Check manual capture flag
            manual_requested = False
            with state.cmd_lock:
                if state.command.capture:
                    manual_requested = True
                    state.command.capture = False

            # Find best detection
            best_det = None
            best_score = 0.0
            for det, score in scored:
                if score > best_score:
                    best_score = score
                    best_det = det

            now = time.time()

            # Auto-capture on good QR if cooldown has elapsed
            auto_captured = False
            if (best_det is not None
                    and best_score >= QR_QUALITY_THRESHOLD
                    and (now - self._last_auto_capture) >= QR_AUTO_CAPTURE_COOLDOWN_S):
                self._capture(clean_frame, best_det, best_score, "auto_qr")
                self._last_auto_capture = now
                auto_captured = True

            # Manual capture — skip if auto already saved the same detection this cycle
            if manual_requested:
                if best_det is not None and not auto_captured:
                    self._capture(clean_frame, best_det, best_score, "manual_qr")
                elif best_det is None:
                    path = _save_still(clean_frame, self._save_dir, "manual_noqr")
                    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ts = time.time()
                    self._b3_log_fn(path, "", ts)
                    state.add_qr("", path, ts_str)
                    log.info("Manual capture (no QR): %s", path)

            elapsed = time.monotonic() - t0
            sleep_for = interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _capture(self, frame: np.ndarray, det, score: float, suffix: str) -> None:
        """Save clean still, call logger callback, and update shared state."""
        qr_text = det.data.decode("utf-8", errors="replace")
        path = _save_still(frame, self._save_dir, suffix)
        ts = time.time()
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        self._b3_log_fn(path, qr_text, ts)
        self._state.add_qr(qr_text, path, ts_str)
        log.info("Captured [%s] Q=%.2f → %s", qr_text, score, path)


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s")

    # Minimal stub imports for standalone run
    from shared_state import AppState

    captures_dir = "/tmp/qr_test_captures"
    os.makedirs(captures_dir, exist_ok=True)

    captured: list[dict] = []

    def fake_log_fn(image_path: str, qr_text: str, timestamp: float):
        captured.append({"path": image_path, "text": qr_text, "ts": timestamp})
        print(f"  [LOG] QR captured: {qr_text!r}  →  {image_path}")

    state = AppState()
    pipeline = QRPipeline()
    pipeline.start(state, fake_log_fn, captures_dir)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("No webcam found — exiting standalone test.")
        pipeline.stop()
        sys.exit(1)

    print("QR pipeline running. SPACE = manual capture, Q = quit.")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            with state.frame_lock:
                state.latest_frame = frame.copy()

            # Show the annotated frame that the pipeline has written back
            with state.frame_lock:
                display = state.latest_frame.copy() if state.latest_frame is not None else frame

            cv2.imshow("QR Pipeline — SPACE: capture  Q: quit", display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key == ord(" "):
                with state.cmd_lock:
                    state.command.capture = True
    finally:
        pipeline.stop()
        cap.release()
        cv2.destroyAllWindows()
        print(f"\nTotal captures: {len(captured)}")
        for c in captured:
            print(f"  {c['text']!r:30s}  {c['path']}")
