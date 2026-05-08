"""
B2 - Vision Processing / QR Pipeline Module
UKSEDS ORT Base Station Software
=====================================
Handles:
  - Real-time QR detection on live video frames (≥5 FPS per PR-05)
  - QR decoding via pyzbar
  - Visual overlays: bounding box + decoded text drawn onto frames
  - Quality scoring to select the best frame for submission
  - Auto-capture: saves high-res still when a good QR is detected
  - Triggered capture: saves still when operator presses SPACE (via B1 flag)
  - Passes decoded QR content + saved image path to B3 for logging

Integration with B1:
  - Reads  state.latest_frame      (numpy BGR frame from video_receiver)
  - Reads  state.command.capture   (True when operator triggers still capture)
  - Writes state.qr_overlay        (last decoded QR string shown on video)

Integration with B3:
  - Calls  b3.log_qr_capture(image_path, qr_text, timestamp)

Dependencies:
    pip install opencv-python pyzbar numpy

Usage:
    Instantiate QRPipeline and call start(state, b3_logger).
    It runs in its own thread alongside the B1 render loop.
"""

import cv2
import threading
import time
import os
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Callable
from pyzbar import pyzbar


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

PROCESS_FPS         = 10       # Max frames processed per second by QR pipeline
QUALITY_THRESHOLD   = 0.55     # Minimum quality score to trigger auto-capture
AUTO_CAPTURE_COOLDOWN = 3.0    # Seconds between successive auto-captures
STILL_SAVE_DIR      = "captures"  # Subdirectory for saved stills (created by B3)
OVERLAY_COLOUR      = (0, 220, 120)   # BGR green for QR bounding box
OVERLAY_WARN        = (30, 180, 255)  # BGR amber for low-quality detections


# ─────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────

@dataclass
class QRDetection:
    """A single QR code found in a frame."""
    data:       str             # Decoded text content
    polygon:    list            # List of (x, y) corner points
    rect:       tuple           # (x, y, w, h) bounding rect
    quality:    float           # 0.0 – 1.0 quality score
    timestamp:  float = field(default_factory=time.time)


@dataclass
class PipelineResult:
    """Output of one pipeline pass on a frame."""
    detections:     list            # List[QRDetection]
    annotated_frame: np.ndarray     # Frame with overlays drawn
    best:           Optional[object] = None   # Highest-quality QRDetection


# ─────────────────────────────────────────────
# Quality Scoring
# ─────────────────────────────────────────────

def score_detection(det, frame_shape: tuple) -> float:
    """
    Score a pyzbar detection 0.0 – 1.0 based on:
      - Bounding box area relative to frame  (bigger = more readable)
      - Aspect ratio of bounding box         (square = better)
      - Polygon convexity                    (undistorted = better)
    """
    fh, fw = frame_shape[:2]
    frame_area = fw * fh
    if frame_area == 0:
        return 0.0

    rect = det.rect
    box_area = rect.width * rect.height

    # 1. Size score: reward QR codes that fill ≥5% of the frame
    size_score = min(1.0, (box_area / frame_area) / 0.05)

    # 2. Aspect score: bounding box should be roughly square
    if rect.height > 0:
        aspect = rect.width / rect.height
        aspect_score = 1.0 - abs(1.0 - aspect) * 0.5
        aspect_score = max(0.0, min(1.0, aspect_score))
    else:
        aspect_score = 0.0

    # 3. Polygon score: reward convex, non-degenerate polygons
    pts = np.array([(p.x, p.y) for p in det.polygon], dtype=np.float32)
    if len(pts) >= 4:
        hull = cv2.convexHull(pts)
        hull_area = cv2.contourArea(hull)
        poly_area = cv2.contourArea(pts)
        if hull_area > 0:
            convexity = poly_area / hull_area
        else:
            convexity = 0.0
        poly_score = convexity
    else:
        poly_score = 0.5

    # Weighted combination
    score = 0.5 * size_score + 0.25 * aspect_score + 0.25 * poly_score
    return round(min(1.0, max(0.0, score)), 3)


# ─────────────────────────────────────────────
# Frame Annotation
# ─────────────────────────────────────────────

def annotate_frame(frame: np.ndarray, detections: list) -> np.ndarray:
    """Draw bounding polygons, quality bars, and decoded text onto frame."""
    out = frame.copy()
    for det in detections:
        colour = OVERLAY_COLOUR if det.quality >= QUALITY_THRESHOLD else OVERLAY_WARN

        # Polygon outline
        pts = np.array(det.polygon, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(out, [pts], isClosed=True, color=colour, thickness=2)

        # Corner dots
        for pt in det.polygon:
            cv2.circle(out, pt, 4, colour, -1)

        # Label background + text
        x, y, w, h = det.rect
        label = f"{det.data[:30]}{'...' if len(det.data) > 30 else ''}"
        qual  = f"Q:{det.quality:.2f}"
        font  = cv2.FONT_HERSHEY_SIMPLEX
        (lw, lh), _ = cv2.getTextSize(label, font, 0.5, 1)
        cv2.rectangle(out, (x, y - lh - 14), (x + max(lw, 60) + 6, y), (0, 0, 0), -1)
        cv2.putText(out, label, (x + 3, y - lh - 2), font, 0.5, colour, 1, cv2.LINE_AA)
        cv2.putText(out, qual,  (x + 3, y - 2),       font, 0.4, colour, 1, cv2.LINE_AA)

        # Quality bar (bottom of bounding box)
        bar_w = w
        filled = int(bar_w * det.quality)
        cv2.rectangle(out, (x, y + h + 2), (x + bar_w, y + h + 8), (40, 40, 40), -1)
        cv2.rectangle(out, (x, y + h + 2), (x + filled, y + h + 8), colour, -1)

    # Crosshair guide for alignment
    fh, fw = out.shape[:2]
    cx, cy = fw // 2, fh // 2
    guide_colour = (60, 60, 60)
    cv2.line(out, (cx - 20, cy), (cx + 20, cy), guide_colour, 1)
    cv2.line(out, (cx, cy - 20), (cx, cy + 20), guide_colour, 1)
    cv2.circle(out, (cx, cy), 40, guide_colour, 1)

    return out


# ─────────────────────────────────────────────
# Core Pipeline
# ─────────────────────────────────────────────

def process_frame(frame: np.ndarray) -> PipelineResult:
    """
    Run QR detection + scoring on a single frame.
    Returns annotated frame and list of QRDetection objects.
    """
    # pyzbar works on greyscale; improves speed and accuracy
    grey = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

    # Adaptive threshold to handle uneven lighting on terrain
    grey_eq = cv2.equalizeHist(grey)

    raw = pyzbar.decode(grey_eq)

    detections = []
    for obj in raw:
        try:
            text = obj.data.decode("utf-8").strip()
        except Exception:
            text = obj.data.decode("latin-1", errors="replace").strip()

        if not text:
            continue

        polygon = [(p.x, p.y) for p in obj.polygon]
        rect    = (obj.rect.left, obj.rect.top, obj.rect.width, obj.rect.height)
        quality = score_detection(obj, frame.shape)

        detections.append(QRDetection(
            data=text, polygon=polygon, rect=rect, quality=quality
        ))

    # Sort best first
    detections.sort(key=lambda d: d.quality, reverse=True)
    best = detections[0] if detections else None

    annotated = annotate_frame(frame, detections)

    return PipelineResult(detections=detections, annotated_frame=annotated, best=best)


# ─────────────────────────────────────────────
# Still Image Saving
# ─────────────────────────────────────────────

def save_still(frame: np.ndarray, tag: str = "") -> str:
    """
    Save a high-quality JPEG still to STILL_SAVE_DIR.
    Returns the saved file path.
    """
    os.makedirs(STILL_SAVE_DIR, exist_ok=True)
    ts   = time.strftime("%Y%m%d_%H%M%S")
    name = f"still_{ts}{'_' + tag if tag else ''}.jpg"
    path = os.path.join(STILL_SAVE_DIR, name)
    # Convert RGB → BGR for OpenCV save
    bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    cv2.imwrite(path, bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return path


# ─────────────────────────────────────────────
# Pipeline Thread
# ─────────────────────────────────────────────

class QRPipeline:
    """
    Runs the QR processing loop in a background thread.
    Reads frames from shared AppState and writes overlay + decoded text back.
    Calls b3_log_fn(image_path, qr_text, timestamp) on successful captures.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_auto_capture = 0.0
        self.last_result: Optional[PipelineResult] = None
        self.result_lock = threading.Lock()

    def start(self, state, b3_log_fn: Callable):
        """
        state       : AppState instance from b1_operator_interface
        b3_log_fn   : callable(image_path: str, qr_text: str, timestamp: float)
        """
        self._running = True
        self._thread  = threading.Thread(
            target=self._run, args=(state, b3_log_fn), daemon=True
        )
        self._thread.start()
        print("[B2] QR pipeline started.")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        print("[B2] QR pipeline stopped.")

    def _run(self, state, b3_log_fn: Callable):
        interval = 1.0 / PROCESS_FPS

        while self._running:
            t0 = time.time()

            # Grab latest frame safely
            with state.frame_lock:
                frame = state.latest_frame
                if frame is None:
                    time.sleep(interval)
                    continue
                frame = frame.copy()

            # Check operator-triggered capture flag
            triggered = False
            with state.cmd_lock:
                if state.command.capture:
                    triggered = True
                    state.command.capture = False   # Consume the flag

            # Run QR pipeline
            result = process_frame(frame)

            # Write annotated frame back so B1 displays it
            with state.frame_lock:
                state.latest_frame = result.annotated_frame

            # Update QR overlay text for B1 HUD
            if result.best:
                state.qr_overlay = result.best.data

            # ── Auto-capture: good QR detected ────────────────────────
            now = time.time()
            if (result.best
                    and result.best.quality >= QUALITY_THRESHOLD
                    and now - self._last_auto_capture > AUTO_CAPTURE_COOLDOWN):

                path = save_still(frame, tag="auto_qr")
                self._last_auto_capture = now
                b3_log_fn(path, result.best.data, now)
                print(f"[B2] Auto-capture: {result.best.data!r}  Q={result.best.quality:.2f}  → {path}")

            # ── Operator-triggered capture ─────────────────────────────
            if triggered:
                tag  = f"manual_{'qr' if result.best else 'noqr'}"
                path = save_still(frame, tag=tag)
                qr   = result.best.data if result.best else ""
                b3_log_fn(path, qr, now)
                print(f"[B2] Manual capture → {path}  QR={qr!r}")

            # Store result for external inspection
            with self.result_lock:
                self.last_result = result

            # Pace to target FPS
            elapsed = time.time() - t0
            sleep   = max(0.0, interval - elapsed)
            time.sleep(sleep)


# ─────────────────────────────────────────────
# Standalone test (no rover needed)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    """
    Standalone test: opens your webcam, runs the QR pipeline,
    and displays the annotated feed. Press SPACE to trigger a manual
    capture, Q to quit.
    """
    import sys

    print("[B2] Standalone test mode — webcam QR scanner")
    print("     SPACE = manual capture  |  Q = quit")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[B2] ERROR: Could not open webcam.")
        sys.exit(1)

    captured = []

    def dummy_log(path, qr, ts):
        captured.append({"path": path, "qr": qr, "ts": ts})
        print(f"[B2-LOG] Saved: {path}  QR={qr!r}")

    # Minimal stub matching the AppState interface
    class _Lock:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class _State:
        latest_frame = None
        qr_overlay   = None
        frame_lock   = _Lock()
        cmd_lock     = _Lock()
        class command:
            capture = False

    state = _State()

    pipeline = QRPipeline()
    pipeline.start(state, dummy_log)

    while True:
        ret, bgr = cap.read()
        if not ret:
            break

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        state.latest_frame = rgb

        # Display annotated frame
        with state.frame_lock:
            display = state.latest_frame
        if display is not None:
            cv2.imshow("B2 QR Pipeline", cv2.cvtColor(display, cv2.COLOR_RGB2BGR))

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        if key == ord(' '):
            state.command.capture = True

    pipeline.stop()
    cap.release()
    cv2.destroyAllWindows()
    print(f"[B2] Session ended. {len(captured)} captures saved.")
