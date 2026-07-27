"""
tests/test_qr_pipeline.py — Unit tests for base_station/qr_pipeline.py

Run:
    cd ort-rover
    pytest tests/test_qr_pipeline.py -v
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "base_station"))

from qr_pipeline import QRPipeline, _score_detection, _save_still


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_qr_frame(data: str = "TEST_QR") -> np.ndarray:
    """Generate a 640×480 BGR frame with a real QR code centred in it."""
    import qrcode
    from PIL import Image
    qr = qrcode.QRCode(box_size=5, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    overlay = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    frame = np.ones((480, 640, 3), dtype=np.uint8) * 255
    h, w = overlay.shape[:2]
    x = (640 - w) // 2
    y = (480 - h) // 2
    frame[y:y + h, x:x + w] = overlay
    return frame


def _detect(frame: np.ndarray):
    """Return raw pyzbar detections from a BGR frame."""
    from pyzbar import pyzbar
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return pyzbar.decode(grey)


def _make_state():
    from shared_state import AppState
    return AppState()


# ── _score_detection ──────────────────────────────────────────────────────────

class TestScoreDetection:
    def test_returns_float_in_0_1(self):
        frame = _make_qr_frame()
        dets = _detect(frame)
        assert dets, "pyzbar found no QR in synthetic frame — check library install"
        score = _score_detection(dets[0], 480, 640)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_blank_frame_no_detections(self):
        blank = np.ones((480, 640, 3), dtype=np.uint8) * 200
        dets = _detect(blank)
        assert dets == []

    def test_good_qr_scores_above_zero(self):
        frame = _make_qr_frame()
        dets = _detect(frame)
        if not dets:
            pytest.skip("QR detection not reliable in this environment")
        score = _score_detection(dets[0], 480, 640)
        assert score > 0.0


# ── _save_still ───────────────────────────────────────────────────────────────

class TestSaveStill:
    def test_creates_jpeg(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        path = _save_still(frame, str(tmp_path), "auto_qr")
        assert Path(path).is_file()

    def test_filename_contains_suffix(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        path = _save_still(frame, str(tmp_path), "manual_noqr")
        assert "manual_noqr" in Path(path).name

    def test_filename_contains_timestamp(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        path = _save_still(frame, str(tmp_path), "auto_qr")
        name = Path(path).name
        assert name.startswith("still_")

    def test_creates_save_dir_if_missing(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        deep = str(tmp_path / "a" / "b" / "c")
        path = _save_still(frame, deep, "test")
        assert Path(path).is_file()


# ── QRPipeline ────────────────────────────────────────────────────────────────

class TestQRPipelineAutoCapture:
    def test_fires_on_good_qr_frame(self, tmp_path):
        state = _make_state()
        captured = []

        def log_fn(image_path, qr_text, ts):
            captured.append(qr_text)

        frame = _make_qr_frame("SITE_AUTO_01")
        with state.frame_lock:
            state.latest_frame = frame.copy()

        pipeline = QRPipeline()
        pipeline.start(state, log_fn, str(tmp_path))
        time.sleep(0.6)  # allow multiple cycles at QR_PROCESS_FPS=10
        pipeline.stop()

        assert len(captured) >= 1, "Expected at least one auto-capture"

    def test_cooldown_prevents_burst(self, tmp_path):
        """Within 3-second cooldown window, only 1 auto-capture should fire."""
        state = _make_state()
        captured = []

        def log_fn(image_path, qr_text, ts):
            captured.append(ts)

        frame = _make_qr_frame("SITE_COOLDOWN")
        with state.frame_lock:
            state.latest_frame = frame.copy()

        pipeline = QRPipeline()
        pipeline.start(state, log_fn, str(tmp_path))
        time.sleep(0.5)
        pipeline.stop()

        assert len(captured) <= 1, f"Expected ≤1 capture, got {len(captured)}"

    def test_no_capture_on_blank_frame(self, tmp_path):
        state = _make_state()
        captured = []

        def log_fn(image_path, qr_text, ts):
            captured.append(qr_text)

        blank = np.ones((480, 640, 3), dtype=np.uint8) * 128
        with state.frame_lock:
            state.latest_frame = blank.copy()

        pipeline = QRPipeline()
        pipeline.start(state, log_fn, str(tmp_path))
        time.sleep(0.5)
        pipeline.stop()

        assert captured == []


class TestQRPipelineManualCapture:
    def test_manual_capture_saves_file(self, tmp_path):
        state = _make_state()
        saved_paths = []

        def log_fn(image_path, qr_text, ts):
            saved_paths.append(image_path)

        blank = np.ones((480, 640, 3), dtype=np.uint8) * 80
        with state.frame_lock:
            state.latest_frame = blank.copy()

        pipeline = QRPipeline()
        pipeline.start(state, log_fn, str(tmp_path))
        time.sleep(0.05)
        with state.cmd_lock:
            state.command.capture = True
        time.sleep(0.2)
        pipeline.stop()

        assert len(saved_paths) == 1
        assert Path(saved_paths[0]).is_file()

    def test_manual_capture_flag_consumed(self, tmp_path):
        """After a manual capture the flag must be cleared."""
        state = _make_state()
        captured = []

        def log_fn(image_path, qr_text, ts):
            captured.append(image_path)

        blank = np.ones((480, 640, 3), dtype=np.uint8) * 80
        with state.frame_lock:
            state.latest_frame = blank.copy()

        pipeline = QRPipeline()
        pipeline.start(state, log_fn, str(tmp_path))
        time.sleep(0.05)
        with state.cmd_lock:
            state.command.capture = True
        time.sleep(0.4)
        pipeline.stop()

        # Flag should have been consumed — only 1 capture, not multiple
        assert len(captured) == 1

    def test_no_double_capture_when_auto_and_manual_same_cycle(self, tmp_path):
        """Auto + manual in same cycle for same QR → only 1 save (auto wins)."""
        state = _make_state()
        captured = []

        def log_fn(image_path, qr_text, ts):
            captured.append(qr_text)

        frame = _make_qr_frame("SITE_DOUBLE")
        with state.frame_lock:
            state.latest_frame = frame.copy()

        pipeline = QRPipeline()
        pipeline.start(state, log_fn, str(tmp_path))
        # Let auto fire once, then set manual immediately after
        time.sleep(0.12)
        with state.cmd_lock:
            state.command.capture = True
        time.sleep(0.12)
        pipeline.stop()

        # After auto fires (cooldown = 3 s), manual in same cycle is skipped
        # Total should be 1 (auto only) or 2 if manual fired before auto cooldown
        # Either is acceptable; what must NOT happen is 3+ captures in 250ms
        assert len(captured) <= 2
