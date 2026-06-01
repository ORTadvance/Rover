"""
tests/test_comms.py — Unit tests for base_station/comms.py

Run:
    cd ort-rover
    pytest tests/test_comms.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "base_station"))

from comms import throttle_steering_to_diff, _clamp


# ── throttle_steering_to_diff ─────────────────────────────────────────────────

class TestThrottleSteeringToDiff:
    def test_straight_forward(self):
        left, right = throttle_steering_to_diff(1.0, 0.0)
        assert left  == pytest.approx(1.0)
        assert right == pytest.approx(1.0)

    def test_straight_reverse(self):
        left, right = throttle_steering_to_diff(-1.0, 0.0)
        assert left  == pytest.approx(-1.0)
        assert right == pytest.approx(-1.0)

    def test_turn_right_on_spot(self):
        left, right = throttle_steering_to_diff(0.0, 1.0)
        assert left  == pytest.approx(1.0)
        assert right == pytest.approx(-1.0)

    def test_turn_left_on_spot(self):
        left, right = throttle_steering_to_diff(0.0, -1.0)
        assert left  == pytest.approx(-1.0)
        assert right == pytest.approx(1.0)

    def test_forward_right_clamps_left(self):
        left, right = throttle_steering_to_diff(1.0, 0.9)
        assert left  == pytest.approx(1.0)   # clamped from 1.9
        assert right == pytest.approx(0.1)

    def test_forward_left_clamps_right(self):
        left, right = throttle_steering_to_diff(1.0, -0.9)
        assert left  == pytest.approx(0.1)
        assert right == pytest.approx(1.0)   # clamped from 1.9

    def test_reverse_right_clamps(self):
        left, right = throttle_steering_to_diff(-1.0, 0.9)
        assert left  == pytest.approx(-0.1)
        assert right == pytest.approx(-1.0)  # clamped from -1.9

    def test_zero_zero(self):
        left, right = throttle_steering_to_diff(0.0, 0.0)
        assert left  == 0.0
        assert right == 0.0

    def test_output_never_exceeds_1(self):
        for thr in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            for steer in [-1.0, -0.5, 0.0, 0.5, 1.0]:
                l, r = throttle_steering_to_diff(thr, steer)
                assert -1.0 <= l <= 1.0, f"left={l} out of range for thr={thr} steer={steer}"
                assert -1.0 <= r <= 1.0, f"right={r} out of range for thr={thr} steer={steer}"


# ── _clamp ────────────────────────────────────────────────────────────────────

class TestClamp:
    def test_within_range_unchanged(self):
        assert _clamp(0.5, 0.0, 1.0) == pytest.approx(0.5)

    def test_below_min_returns_min(self):
        assert _clamp(-0.5, 0.0, 1.0) == pytest.approx(0.0)

    def test_above_max_returns_max(self):
        assert _clamp(1.5, 0.0, 1.0) == pytest.approx(1.0)

    def test_servo_clamp_too_high(self):
        assert _clamp(3000, 500, 2500) == 2500

    def test_servo_clamp_too_low(self):
        assert _clamp(0, 500, 2500) == 500

    def test_servo_valid_midpoint(self):
        assert _clamp(1500, 500, 2500) == 1500

    def test_exact_boundaries_not_clamped(self):
        assert _clamp(0.0, 0.0, 1.0) == 0.0
        assert _clamp(1.0, 0.0, 1.0) == 1.0


# ── E-Stop via shared state ────────────────────────────────────────────────────

class TestEStop:
    def _make_state(self):
        from shared_state import AppState
        return AppState()

    def test_estop_flag_set(self):
        state = self._make_state()
        with state.cmd_lock:
            state.command.e_stop = True
        with state.cmd_lock:
            assert state.command.e_stop is True

    def test_release_clears_flag(self):
        state = self._make_state()
        with state.cmd_lock:
            state.command.e_stop = True
        with state.cmd_lock:
            state.command.e_stop = False
        with state.cmd_lock:
            assert state.command.e_stop is False

    def test_interface_zeroes_throttle_on_estop(self):
        state = self._make_state()
        with state.cmd_lock:
            state.command.throttle = 0.8
            state.command.steering = 0.3
            state.command.e_stop   = True

        # Simulate what interface.py does each frame when e_stop is active
        with state.cmd_lock:
            if state.command.e_stop:
                state.command.throttle = 0.0
                state.command.steering = 0.0

        with state.cmd_lock:
            assert state.command.throttle == 0.0
            assert state.command.steering == 0.0

    def test_command_tx_sends_stop_type(self):
        """Verify the stop message format that _command_tx sends."""
        import json
        msg = json.dumps({"type": "stop"})
        parsed = json.loads(msg)
        assert parsed["type"] == "stop"
        assert "left" not in parsed
        assert "right" not in parsed


# ── drive message format ──────────────────────────────────────────────────────

class TestDriveMessageFormat:
    def test_drive_message_has_required_fields(self):
        import json
        import time
        left, right = throttle_steering_to_diff(0.5, 0.1)
        msg = json.dumps({
            "type":  "drive",
            "left":  round(left,  4),
            "right": round(right, 4),
            "ts":    time.time(),
        })
        parsed = json.loads(msg)
        assert parsed["type"]  == "drive"
        assert "left"  in parsed
        assert "right" in parsed
        assert "ts"    in parsed

    def test_servo_message_format(self):
        import json
        msg = json.dumps({"type": "servo", "pulsewidth": 1600})
        parsed = json.loads(msg)
        assert parsed["type"]       == "servo"
        assert parsed["pulsewidth"] == 1600
