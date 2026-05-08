"""
B3 - Mission Management and Logging Module
UKSEDS ORT Base Station Software
=====================================
Handles:
  - Logging every QR capture (image path, decoded text, timestamp) per site
  - Logging operator actions (commands, events) with timestamps
  - Organising captures into a structured per-mission directory
  - Exporting a mission output package (JSON manifest + images) for submission
  - Providing a simple summary printout at mission end

Integration with B2:
  - B2 calls b3.log_qr_capture(image_path, qr_text, timestamp)

Integration with B1:
  - B1 calls b3.log_action(action, detail) for key operator events
  - B1 calls b3.export() at mission end (ESC / quit)

Output structure:
  missions/
    YYYYMMDD_HHMMSS/          ← one folder per mission run
      captures/               ← JPEG stills (copied here by B3)
        still_....jpg
      mission_log.json        ← full structured log
      submission_package.json ← clean export for judges

Dependencies:
    pip install (none beyond stdlib + shutil)

Usage:
    logger = MissionLogger()
    logger.start()
    ...
    logger.log_qr_capture(path, qr_text, timestamp)
    logger.log_action("ESTOP", "operator triggered")
    ...
    export_path = logger.export()
"""

import json
import os
import shutil
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

MISSIONS_ROOT   = "missions"     # Top-level directory for all mission runs
CAPTURES_SUBDIR = "captures"     # Subdirectory inside mission folder for images
LOG_FILENAME    = "mission_log.json"
EXPORT_FILENAME = "submission_package.json"
TEAM_NAME       = "UKSEDS ORT Team"   # Edit as needed
COMPETITION     = "UKSEDS ORT 2026 — Basic Stream"


# ─────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────

@dataclass
class QRCapture:
    """One QR code detection and capture event."""
    site_index:  int
    qr_text:     str
    image_file:  str        # Filename only (relative to mission folder)
    timestamp:   float
    timestamp_str: str = ""

    def __post_init__(self):
        if not self.timestamp_str:
            self.timestamp_str = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)
            )


@dataclass
class ActionEntry:
    """One logged operator or system action."""
    action:    str
    detail:    str
    timestamp: float
    timestamp_str: str = ""

    def __post_init__(self):
        if not self.timestamp_str:
            self.timestamp_str = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)
            )


@dataclass
class MissionRecord:
    """Full mission record written to mission_log.json."""
    team:         str
    competition:  str
    mission_id:   str
    start_time:   float
    start_str:    str
    end_time:     Optional[float]    = None
    end_str:      Optional[str]      = None
    duration_s:   Optional[float]    = None
    qr_captures:  list = field(default_factory=list)   # List[QRCapture]
    action_log:   list = field(default_factory=list)   # List[ActionEntry]


# ─────────────────────────────────────────────
# Mission Logger
# ─────────────────────────────────────────────

class MissionLogger:
    """
    Thread-safe mission logger.
    Call start() once at the beginning of the mission.
    Call export() once at the end to produce the submission package.
    """

    def __init__(self):
        self._lock          = threading.Lock()
        self._record:  Optional[MissionRecord] = None
        self._mission_dir:  Optional[str] = None
        self._captures_dir: Optional[str] = None
        self._site_counter  = 0
        self._started       = False

    # ─────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────

    def start(self, mission_id: Optional[str] = None):
        """
        Initialise the mission folder and record.
        Call once when the operator starts the mission.
        """
        now = time.time()
        if mission_id is None:
            mission_id = time.strftime("%Y%m%d_%H%M%S")

        mission_dir  = os.path.join(MISSIONS_ROOT, mission_id)
        captures_dir = os.path.join(mission_dir, CAPTURES_SUBDIR)
        os.makedirs(captures_dir, exist_ok=True)

        record = MissionRecord(
            team        = TEAM_NAME,
            competition = COMPETITION,
            mission_id  = mission_id,
            start_time  = now,
            start_str   = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        )

        with self._lock:
            self._mission_dir  = mission_dir
            self._captures_dir = captures_dir
            self._record       = record
            self._started      = True

        self._write_log()
        print(f"[B3] Mission started: {mission_dir}")

    def _require_started(self):
        if not self._started:
            raise RuntimeError("[B3] MissionLogger.start() must be called first.")

    # ─────────────────────────────────────────
    # Logging API (called by B2 and B1)
    # ─────────────────────────────────────────

    def log_qr_capture(self, image_path: str, qr_text: str,
                       timestamp: Optional[float] = None):
        """
        Called by B2 whenever a capture is saved.
        Copies the image into the mission captures folder.
        """
        self._require_started()
        if timestamp is None:
            timestamp = time.time()

        # Copy image into mission folder
        filename = os.path.basename(image_path)
        dest     = os.path.join(self._captures_dir, filename)
        try:
            if os.path.abspath(image_path) != os.path.abspath(dest):
                shutil.copy2(image_path, dest)
        except FileNotFoundError:
            print(f"[B3] WARNING: Image not found: {image_path}")

        with self._lock:
            self._site_counter += 1
            entry = QRCapture(
                site_index = self._site_counter,
                qr_text    = qr_text,
                image_file = os.path.join(CAPTURES_SUBDIR, filename),
                timestamp  = timestamp,
            )
            self._record.qr_captures.append(asdict(entry))

        self._write_log()
        print(f"[B3] QR capture #{self._site_counter} logged: {qr_text!r}")

    def log_action(self, action: str, detail: str = "",
                   timestamp: Optional[float] = None):
        """
        Called by B1 for key operator events:
          e.g. log_action("ESTOP", "operator triggered")
               log_action("LINK_LOST", "no telemetry for 3s")
               log_action("MISSION_START", "")
        """
        self._require_started()
        if timestamp is None:
            timestamp = time.time()

        with self._lock:
            entry = ActionEntry(action=action, detail=detail, timestamp=timestamp)
            self._record.action_log.append(asdict(entry))

        self._write_log()

    # ─────────────────────────────────────────
    # Internal write
    # ─────────────────────────────────────────

    def _write_log(self):
        """Write current record to mission_log.json (called after every update)."""
        with self._lock:
            record_dict = asdict(self._record)
            path = os.path.join(self._mission_dir, LOG_FILENAME)

        with open(path, "w") as f:
            json.dump(record_dict, f, indent=2)

    # ─────────────────────────────────────────
    # Export
    # ─────────────────────────────────────────

    def export(self) -> str:
        """
        Finalise the mission and write the submission package JSON.
        Returns the path to the export file.
        Call once when the mission ends (operator quits B1).
        """
        self._require_started()
        now = time.time()

        with self._lock:
            self._record.end_time   = now
            self._record.end_str    = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now)
            )
            self._record.duration_s = round(now - self._record.start_time, 1)

        # Build clean submission package
        with self._lock:
            captures = list(self._record.qr_captures)
            record   = asdict(self._record)

        # Deduplicate QR texts (keep first occurrence per unique QR value)
        seen   = set()
        unique = []
        for c in captures:
            if c["qr_text"] not in seen:
                seen.add(c["qr_text"])
                unique.append(c)

        package = {
            "submission": {
                "team":        TEAM_NAME,
                "competition": COMPETITION,
                "mission_id":  record["mission_id"],
                "start":       record["start_str"],
                "end":         record["end_str"],
                "duration_s":  record["duration_s"],
            },
            "qr_results": [
                {
                    "site":       c["site_index"],
                    "qr_text":    c["qr_text"],
                    "image_file": c["image_file"],
                    "captured_at": c["timestamp_str"],
                }
                for c in unique
            ],
            "total_sites_captured": len(unique),
            "total_captures":       len(captures),
            "action_log":           record["action_log"],
        }

        export_path = os.path.join(self._mission_dir, EXPORT_FILENAME)
        with open(export_path, "w") as f:
            json.dump(package, f, indent=2)

        # Final log write
        self._write_log()

        self._print_summary(package)
        return export_path

    # ─────────────────────────────────────────
    # Summary
    # ─────────────────────────────────────────

    def _print_summary(self, package: dict):
        sub      = package["submission"]
        results  = package["qr_results"]
        dur_min  = sub["duration_s"] / 60 if sub["duration_s"] else 0

        print("\n" + "=" * 50)
        print("  MISSION SUMMARY")
        print("=" * 50)
        print(f"  Team:       {sub['team']}")
        print(f"  Mission ID: {sub['mission_id']}")
        print(f"  Duration:   {dur_min:.1f} min ({sub['duration_s']}s)")
        print(f"  Sites captured (unique): {package['total_sites_captured']}")
        print(f"  Total captures:          {package['total_captures']}")
        print("-" * 50)
        for r in results:
            print(f"  Site {r['site']:>2}: {r['qr_text'][:50]}")
        print("=" * 50)
        print(f"  Export → {os.path.join(MISSIONS_ROOT, sub['mission_id'], EXPORT_FILENAME)}")
        print("=" * 50 + "\n")

    # ─────────────────────────────────────────
    # Utility
    # ─────────────────────────────────────────

    @property
    def capture_count(self) -> int:
        with self._lock:
            return len(self._record.qr_captures) if self._record else 0

    @property
    def mission_dir(self) -> Optional[str]:
        return self._mission_dir


# ─────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    """
    Simulate a short mission with a few QR captures and actions.
    Inspect the output in missions/<timestamp>/
    """
    import tempfile, sys

    print("[B3] Standalone test — simulating mission")

    logger = MissionLogger()
    logger.start()

    logger.log_action("MISSION_START", "operator initiated")
    time.sleep(0.2)

    # Simulate B2 calling log_qr_capture (using a temp image)
    for i, qr_text in enumerate(["SITE_ALPHA_42", "SITE_BETA_07", "SITE_ALPHA_42"]):
        # Create a dummy JPEG for testing
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # Minimal JPEG header
        tmp.close()
        logger.log_qr_capture(tmp.name, qr_text)
        logger.log_action("CAPTURE", f"site {i+1} image saved")
        time.sleep(0.1)

    logger.log_action("ESTOP", "test E-stop event")
    time.sleep(0.1)
    logger.log_action("MISSION_END", "operator quit")

    export_path = logger.export()
    print(f"[B3] Test complete. Check: {export_path}")
