import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import config as _cfg_module

log = logging.getLogger("logger")  # root-logger setup belongs in main.py, not here


class MissionLogger:
    def __init__(self):
        self._lock = threading.Lock()
        self._mission_dir: Path | None = None
        self._mission_id: str = ""
        self._start_time: float | None = None  # None = not started; avoids 0.0 sentinel ambiguity
        self._qr_captures: list[dict] = []
        self._action_log: list[dict] = []
        self._site_counter: int = 0

    def start(self, mission_id: str = None) -> None:
        with self._lock:
            self._mission_id = mission_id or datetime.now().strftime("%Y%m%d_%H%M%S")
            # Resolve to absolute path at start() time so CWD changes don't break later I/O.
            self._mission_dir = Path(_cfg_module.MISSIONS_ROOT).resolve() / self._mission_id
            (self._mission_dir / "captures").mkdir(parents=True, exist_ok=True)
            self._start_time = time.monotonic()
            self._qr_captures = []
            self._action_log = []
            self._site_counter = 0
            doc = self._build_log_doc()
        # Write outside the lock so disk I/O doesn't block other callers.
        self._flush_log(doc)
        log.info("Mission started: %s", self._mission_id)

    def log_qr_capture(
        self, image_path: str, qr_text: str, timestamp: float = None
    ) -> None:
        if self._mission_dir is None:
            log.warning("log_qr_capture(%s) called before start(); event dropped", qr_text)
            return

        ts = time.time() if timestamp is None else timestamp  # 'or' would drop a valid 0.0
        timestamp_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            self._site_counter += 1
            site_index = self._site_counter

            dest_path = ""
            if image_path and os.path.isfile(image_path):
                dest = self._mission_dir / "captures" / Path(image_path).name
                try:
                    shutil.copy2(image_path, dest)
                    dest_path = str(Path("captures") / Path(image_path).name)
                except OSError as exc:
                    log.warning("Failed to copy capture %s: %s", image_path, exc)
            else:
                log.warning("Capture image not found, logging without file: %s", image_path)

            self._qr_captures.append(
                {
                    "site_index": site_index,
                    "qr_text": qr_text,
                    "image_file": dest_path,
                    "timestamp_str": timestamp_str,
                }
            )
            doc = self._build_log_doc()
        self._flush_log(doc)

        log.info("QR capture #%d logged: %s", site_index, qr_text)

    def log_action(
        self, action: str, detail: str = "", timestamp: float = None
    ) -> None:
        if self._mission_dir is None:
            log.warning("log_action(%s) called before start(); event dropped", action)
            return

        ts = time.time() if timestamp is None else timestamp  # 'or' would drop a valid 0.0
        timestamp_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            self._action_log.append(
                {"action": action, "detail": detail, "timestamp_str": timestamp_str}
            )
            doc = self._build_log_doc()
        self._flush_log(doc)

        log.info("Action: %s %s", action, detail)

    def export(self) -> str:
        with self._lock:
            end_time = time.time()
            end_str = datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S")
            elapsed = (
                time.monotonic() - self._start_time
                if self._start_time is not None
                else 0.0
            )

            seen: set[str] = set()
            unique_results = []
            for cap in self._qr_captures:
                if cap["qr_text"] not in seen:
                    seen.add(cap["qr_text"])
                    unique_results.append(
                        {
                            "site": cap["site_index"],
                            "qr_text": cap["qr_text"],
                            "image_file": cap["image_file"],
                            "captured_at": cap["timestamp_str"],
                        }
                    )

            package = {
                "submission": {
                    "team": _cfg_module.TEAM_NAME,
                    "competition": _cfg_module.COMPETITION,
                    "mission_id": self._mission_id,
                    "start": self._start_str(),
                    "end": end_str,
                    "duration_s": round(elapsed, 2),
                },
                "qr_results": unique_results,
                "total_unique_sites": len(unique_results),
                "total_captures": len(self._qr_captures),
                "action_log": list(self._action_log),
            }

            log_doc = self._build_log_doc(end_str=end_str, duration_s=round(elapsed, 2))
            out_path = self._mission_dir / "submission_package.json"

        # Both writes happen outside the lock and are each individually atomic.
        self._flush_to(out_path, package)
        self._flush_log(log_doc)

        log.info("Exported submission package: %s", out_path)
        return str(out_path)

    @property
    def mission_dir(self) -> str:
        return str(self._mission_dir) if self._mission_dir else ""

    @property
    def captures_dir(self) -> str:
        return str(self._mission_dir / "captures") if self._mission_dir else ""

    @property
    def mission_elapsed_s(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def capture_count(self) -> int:
        with self._lock:
            return len(self._qr_captures)

    @property
    def unique_qr_count(self) -> int:
        with self._lock:
            return len({c["qr_text"] for c in self._qr_captures})

    @property
    def summary_dict(self) -> dict:
        with self._lock:
            # Compute elapsed inline so _start_time is read under the lock.
            elapsed = (
                time.monotonic() - self._start_time
                if self._start_time is not None
                else 0.0
            )
            return {
                "captures": len(self._qr_captures),
                "unique_qr": len({c["qr_text"] for c in self._qr_captures}),
                "elapsed_s": round(elapsed, 1),
            }

    def _start_str(self) -> str:
        """Reconstruct wall-clock start time from monotonic reference. Caller holds lock."""
        if self._start_time is None:
            return ""
        wall = time.time() - (time.monotonic() - self._start_time)
        return datetime.fromtimestamp(wall).strftime("%Y-%m-%d %H:%M:%S")

    def _build_log_doc(
        self, end_str: str = None, duration_s: float = None
    ) -> dict:
        """Snapshot current state into a log document. Must be called with self._lock held."""
        return {
            "team": _cfg_module.TEAM_NAME,
            "competition": _cfg_module.COMPETITION,
            "mission_id": self._mission_id,
            "start_str": self._start_str(),
            "end_str": end_str,
            "duration_s": duration_s,
            "qr_captures": list(self._qr_captures),
            "action_log": list(self._action_log),
        }

    def _flush_log(self, doc: dict) -> None:
        """Atomically persist mission_log.json. Called outside lock."""
        if self._mission_dir is None:
            return
        self._flush_to(self._mission_dir / "mission_log.json", doc)

    @staticmethod
    def _flush_to(path: Path, obj: dict) -> None:
        """Write obj as JSON to path atomically using a sibling .tmp + os.replace()."""
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)


if __name__ == "__main__":
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    with tempfile.TemporaryDirectory() as tmp:
        _original_root = _cfg_module.MISSIONS_ROOT
        _cfg_module.MISSIONS_ROOT = tmp

        logger = MissionLogger()

        # Verify pre-start guard: these must be silently dropped, not raise.
        logger.log_action("SHOULD_BE_DROPPED")
        logger.log_qr_capture("/nonexistent.jpg", "SHOULD_BE_DROPPED")

        logger.start("test_mission_001")
        logger.log_action("MISSION_START")

        dummy_images = []
        for i in range(1, 4):
            img = Path(tmp) / f"frame_{i:03d}.jpg"
            img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
            dummy_images.append(str(img))

        logger.log_qr_capture(dummy_images[0], "SITE_ALPHA_01")
        logger.log_action("OBSTACLE_BLOCKED", "ir_front=True")
        logger.log_qr_capture(dummy_images[1], "SITE_BETA_02")
        logger.log_qr_capture(dummy_images[2], "SITE_ALPHA_01")  # duplicate

        # Explicit timestamp=0.0 must be preserved, not replaced with time.time().
        logger.log_action("EPOCH_EVENT", timestamp=0.0)
        expected_epoch_str = datetime.fromtimestamp(0.0).strftime("%Y-%m-%d %H:%M:%S")
        assert logger._action_log[-1]["timestamp_str"] == expected_epoch_str, (
            f"falsy-zero fix broken: got {logger._action_log[-1]['timestamp_str']}"
        )

        export_path = logger.export()

        summary = logger.summary_dict
        print(f"captures     : {summary['captures']}")
        print(f"unique QR    : {summary['unique_qr']}")
        print(f"elapsed_s    : {summary['elapsed_s']}")
        print(f"export path  : {export_path}")

        pkg = json.loads(Path(export_path).read_text())
        assert pkg["total_captures"] == 3, "expected 3 QR captures"
        assert pkg["total_unique_sites"] == 2, "expected 2 unique sites"
        assert len(pkg["qr_results"]) == 2

        # action_log in export: MISSION_START, OBSTACLE_BLOCKED, EPOCH_EVENT = 3
        assert len(pkg["action_log"]) == 3, f"got {len(pkg['action_log'])}"

        log_doc = json.loads(
            (Path(tmp) / "test_mission_001" / "mission_log.json").read_text()
        )
        assert len(log_doc["qr_captures"]) == 3
        assert len(log_doc["action_log"]) == 3

        # Verify atomic write: no .tmp file left behind.
        assert not (Path(tmp) / "test_mission_001" / "mission_log.tmp").exists()
        assert not (Path(tmp) / "test_mission_001" / "submission_package.tmp").exists()

        _cfg_module.MISSIONS_ROOT = _original_root
        print("All assertions passed.")
