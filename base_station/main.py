"""
base_station/main.py — ORT Rover Base Station entry point.

Boots all subsystems in order, wires them together, and handles
graceful shutdown on all exit paths.
"""

import argparse
import logging
import os
import sys
import threading
import time
import traceback


# ── CLI args ──────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ORT Rover Base Station",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--rover-ip",
        default=None,
        metavar="IP",
        help="Override ROVER_IP from config (e.g. 192.168.1.100)",
    )
    parser.add_argument(
        "--mission-id",
        default=None,
        metavar="ID",
        help="Override auto-generated mission folder name",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Launch pygame fullscreen",
    )
    parser.add_argument(
        "--no-gamepad",
        action="store_true",
        help="Disable gamepad detection",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Show [SIMULATION] banner on video panel",
    )
    return parser.parse_args()


# ── Logging setup (called once, after mission dir exists) ─────────────────────
def _setup_logging(log_file: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Could not open session log file %s: %s", log_file, exc)


_log = logging.getLogger("main")


# ── Startup telemetry health check ────────────────────────────────────────────
def _health_check_worker(state, timeout_s: float) -> None:
    """Background daemon thread. Adds a UI alert if no telemetry within timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with state.state_lock:
            last_rx = state.telemetry.last_rx
        if last_rx > 0.0:
            _log.info("Telemetry received — rover is online")
            return
        time.sleep(0.1)
    msg = "No telemetry — check rover connection"
    _log.warning(msg)
    state.add_alert(msg)


# ── Shutdown helper ───────────────────────────────────────────────────────────
def _shutdown(pipeline, comms, logger, *, crash: bool = False) -> None:
    _log.info("Shutting down subsystems...")
    if pipeline is not None:
        try:
            pipeline.stop()
        except Exception:
            _log.exception("Error stopping QR pipeline")
    if comms is not None:
        try:
            comms.stop()
        except Exception:
            _log.exception("Error stopping comms")
    if logger is not None:
        try:
            logger.log_action("MISSION_END" if not crash else "CRASH_SHUTDOWN")
        except Exception:
            _log.exception("Error logging mission end")
        try:
            export_path = logger.export()
            print(f"\nMission saved to: {export_path}")
        except Exception:
            _log.exception("Error exporting mission data")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    args = _parse_args()

    # Override ROVER_IP before importing modules that bind it at load time.
    # comms.py uses `from config import ROVER_IP` at module level, so the
    # mutation must happen before comms is first imported.
    import config as _cfg
    if args.rover_ip:
        _cfg.ROVER_IP = args.rover_ip
        print(f"[main] Rover IP overridden to {args.rover_ip}")

    # Lazy imports so the config mutation above takes effect in comms.py.
    from shared_state import AppState
    from logger import MissionLogger
    from comms import CommsManager
    from qr_pipeline import QRPipeline
    import interface

    # MissionLogger.__init__ is pure (no I/O), so it's safe outside the try.
    logger   = MissionLogger()
    state    = None
    comms    = None
    pipeline = None
    crashed  = False
    exit_code = 0

    try:
        state = AppState()
        logger.start(args.mission_id)
        logger.log_action("MISSION_START")

        # Logging is configured once, after the mission directory exists.
        log_file = os.path.join(logger.mission_dir, "session.log") if logger.mission_dir else ""
        _setup_logging(log_file)
        _log.info("ORT Rover Base Station starting up")
        _log.info("Mission dir: %s", logger.mission_dir or "unknown")
        if args.mock:
            _log.info("Mock/simulation mode enabled")

        comms    = CommsManager()
        pipeline = QRPipeline()

        comms.start(state, logger)

        # Health check runs in a background daemon thread so the UI opens
        # immediately without blocking on the rover connection timeout.
        threading.Thread(
            target=_health_check_worker,
            args=(state, _cfg.LINK_TIMEOUT_S),
            daemon=True,
            name="health-check",
        ).start()

        pipeline.start(state, logger.log_qr_capture, logger.captures_dir)
        _log.info("QR pipeline started")

        _log.info("Starting operator interface")
        interface.run(
            state,
            logger,
            mock_mode=args.mock,
            fullscreen=args.fullscreen,
            no_gamepad=args.no_gamepad,
        )

    except KeyboardInterrupt:
        _log.info("KeyboardInterrupt — shutting down")
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
        _log.info("SystemExit(%s) — shutting down", exit_code)
    except Exception:
        crashed   = True
        exit_code = 1
        crash_tb  = traceback.format_exc()
        _log.critical("Unhandled exception:\n%s", crash_tb)
        if logger.mission_dir:
            try:
                crash_path = os.path.join(logger.mission_dir, "crash.log")
                with open(crash_path, "w", encoding="utf-8") as f:
                    f.write(crash_tb)
                _log.info("Crash log written to %s", crash_path)
            except Exception:
                pass

    _shutdown(pipeline, comms, logger, crash=crashed)
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
