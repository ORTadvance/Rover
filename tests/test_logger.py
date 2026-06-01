"""
tests/test_logger.py — Unit tests for base_station/logger.py

Run:
    cd ort-rover
    pytest tests/test_logger.py -v
"""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "base_station"))

import config as bs_config
from logger import MissionLogger


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_logger(tmp_path):
    """Started MissionLogger writing into a temp directory."""
    original = bs_config.MISSIONS_ROOT
    bs_config.MISSIONS_ROOT = str(tmp_path)
    logger = MissionLogger()
    logger.start("test_mission")
    yield logger, tmp_path
    bs_config.MISSIONS_ROOT = original


def _dummy_jpeg(directory: Path, name: str = "frame.jpg") -> str:
    """Write a minimal valid JPEG stub and return its path."""
    p = directory / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    return str(p)


# ── start() ───────────────────────────────────────────────────────────────────

class TestStart:
    def test_creates_mission_directory(self, tmp_logger):
        logger, tmp_path = tmp_logger
        assert (tmp_path / "test_mission").is_dir()

    def test_creates_captures_subdirectory(self, tmp_logger):
        logger, tmp_path = tmp_logger
        assert (tmp_path / "test_mission" / "captures").is_dir()

    def test_creates_mission_log_json(self, tmp_logger):
        logger, tmp_path = tmp_logger
        log_path = tmp_path / "test_mission" / "mission_log.json"
        assert log_path.is_file()

    def test_mission_log_contains_correct_id(self, tmp_logger):
        logger, tmp_path = tmp_logger
        doc = json.loads((tmp_path / "test_mission" / "mission_log.json").read_text())
        assert doc["mission_id"] == "test_mission"

    def test_mission_elapsed_starts_near_zero(self, tmp_logger):
        logger, _ = tmp_logger
        assert logger.mission_elapsed_s < 1.0


# ── log_qr_capture() ──────────────────────────────────────────────────────────

class TestLogQRCapture:
    def test_increments_capture_count(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path), "SITE_A")
        assert logger.capture_count == 1

    def test_multiple_captures_counted(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        for i in range(3):
            logger.log_qr_capture(_dummy_jpeg(tmp_path, f"f{i}.jpg"), f"SITE_{i}")
        assert logger.capture_count == 3

    def test_copies_image_to_captures_dir(self, tmp_logger, tmp_path):
        logger, base = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path), "SITE_A")
        copies = list((base / "test_mission" / "captures").iterdir())
        assert len(copies) == 1

    def test_missing_image_does_not_raise(self, tmp_logger):
        logger, _ = tmp_logger
        logger.log_qr_capture("/nonexistent/totally_missing.jpg", "SITE_MISSING")
        assert logger.capture_count == 1

    def test_mission_log_updated_immediately(self, tmp_logger, tmp_path):
        logger, base = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path), "SITE_GAMMA")
        doc = json.loads((base / "test_mission" / "mission_log.json").read_text())
        assert len(doc["qr_captures"]) == 1
        assert doc["qr_captures"][0]["qr_text"] == "SITE_GAMMA"

    def test_explicit_zero_timestamp_preserved(self, tmp_logger, tmp_path):
        from datetime import datetime
        logger, _ = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path), "EPOCH", timestamp=0.0)
        expected = datetime.fromtimestamp(0.0).strftime("%Y-%m-%d %H:%M:%S")
        assert logger._qr_captures[-1]["timestamp_str"] == expected


# ── export() ──────────────────────────────────────────────────────────────────

class TestExport:
    def test_produces_submission_package_file(self, tmp_logger, tmp_path):
        logger, base = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path), "SITE_A")
        path = logger.export()
        assert Path(path).is_file()
        assert "submission_package.json" in path

    def test_package_structure(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path), "SITE_A")
        pkg = json.loads(Path(logger.export()).read_text())
        assert "submission" in pkg
        assert "qr_results" in pkg
        assert "total_unique_sites" in pkg
        assert "total_captures" in pkg
        assert "action_log" in pkg

    def test_deduplication_by_qr_text(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        for i in range(3):
            logger.log_qr_capture(_dummy_jpeg(tmp_path, f"f{i}.jpg"), "SAME_SITE")
        pkg = json.loads(Path(logger.export()).read_text())
        assert pkg["total_captures"] == 3
        assert pkg["total_unique_sites"] == 1
        assert len(pkg["qr_results"]) == 1

    def test_deduplication_keeps_first_occurrence(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path, "a.jpg"), "SITE_DUP")
        logger.log_qr_capture(_dummy_jpeg(tmp_path, "b.jpg"), "SITE_DUP")
        pkg = json.loads(Path(logger.export()).read_text())
        assert pkg["qr_results"][0]["site"] == 1  # first capture kept

    def test_no_tmp_files_left_behind(self, tmp_logger, tmp_path):
        logger, base = tmp_logger
        logger.log_qr_capture(_dummy_jpeg(tmp_path), "SITE_X")
        logger.export()
        assert not (base / "test_mission" / "mission_log.tmp").exists()
        assert not (base / "test_mission" / "submission_package.tmp").exists()


# ── unique_qr_count ───────────────────────────────────────────────────────────

class TestUniqueQRCount:
    def test_counts_distinct_texts(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        for site in ["A", "B", "A", "C", "B"]:
            logger.log_qr_capture(_dummy_jpeg(tmp_path, f"{site}x.jpg"), site)
        assert logger.unique_qr_count == 3

    def test_all_same_counts_as_one(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        for i in range(5):
            logger.log_qr_capture(_dummy_jpeg(tmp_path, f"f{i}.jpg"), "SAME")
        assert logger.unique_qr_count == 1


# ── Thread safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_captures_all_logged(self, tmp_logger, tmp_path):
        logger, _ = tmp_logger
        errors = []

        # Create all image files before spawning threads so the test focuses
        # purely on log_qr_capture thread safety, not file-creation ordering.
        image_paths = [_dummy_jpeg(tmp_path, f"thread_{n}.jpg") for n in range(10)]

        def worker(img_path, n):
            try:
                logger.log_qr_capture(img_path, f"SITE_{n}")
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(image_paths[i], i)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert logger.capture_count == 10
