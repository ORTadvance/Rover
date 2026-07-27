# ── Network ────────────────────────────────────────────
ROVER_IP          = "192.168.1.100"  # Pi IP on your router
CMD_PUB_PORT      = 5556             # base PUBs commands → rover SUBs
TELEM_SUB_PORT    = 5557             # base SUBs telemetry ← rover PUSHes
VIDEO_SUB_PORT    = 5558             # base SUBs video ← rover PUSHes

# ── Heartbeat ──────────────────────────────────────────
HEARTBEAT_SEND_HZ    = 2     # how often base sends heartbeat to rover
LINK_TIMEOUT_S       = 3.0   # seconds without telemetry → link lost

# ── Drive command ──────────────────────────────────────
CMD_SEND_HZ          = 20    # drive command transmit rate
DRIVE_SPEED          = 0.6   # default throttle magnitude [0.0–1.0]
TURN_SPEED           = 0.5   # default steering magnitude [0.0–1.0]
SPEED_RAMP_STEP      = 0.05  # max throttle change per command cycle (smoothing)
GAMEPAD_DEADZONE     = 0.08  # ignore stick inputs below this magnitude

# ── Camera servo ───────────────────────────────────────
SERVO_MIN_PW         = 500
SERVO_MID_PW         = 1500
SERVO_MAX_PW         = 2500
SERVO_STEP_PW        = 100   # must match rover config

# ── QR pipeline ────────────────────────────────────────
QR_PROCESS_FPS       = 10    # frames analysed per second by QR pipeline
QR_QUALITY_THRESHOLD = 0.55  # minimum quality score [0.0–1.0] to trigger auto-capture
QR_AUTO_CAPTURE_COOLDOWN_S = 3.0  # minimum seconds between successive auto-captures

# ── Display ────────────────────────────────────────────
WINDOW_W             = 1280
WINDOW_H             = 720
VIDEO_W              = 854
VIDEO_H              = 480
TARGET_FPS           = 30

# ── Colours (dark aerospace theme) ─────────────────────
C_BG                 = (10,  12,  18)
C_PANEL              = (18,  22,  32)
C_BORDER             = (40,  50,  70)
C_ACCENT             = (0,  200, 120)   # green
C_WARN               = (255, 180,  30)  # amber
C_DANGER             = (220,  50,  50)  # red
C_TEXT               = (210, 220, 235)
C_SUBTEXT            = (100, 120, 150)
C_VIDEO_BG           = (5,    8,  14)
C_TAB_ACTIVE         = (0,  200, 120)
C_TAB_INACTIVE       = (40,  50,  70)

# ── Thresholds (used by interface.py for colours and alerts) ───────────────
BATTERY_WARN_V       = 7.0    # amber warning threshold — must match rover config
BATTERY_CRIT_V       = 6.6    # red critical threshold — must match rover config
TEMP_WARN_C          = 60.0   # CPU temp amber threshold
TEMP_CRIT_C          = 75.0   # CPU temp red threshold

# ── Obstacle safety ────────────────────────────────────
OBSTACLE_BLOCK_DRIVE = True   # mirrors rover config — toggle from UI

# ── Mission ────────────────────────────────────────────
MISSIONS_ROOT        = "missions"   # folder where all mission runs are saved
TEAM_NAME            = "QMSEDS — Queen Mary University of London"
COMPETITION          = "UKSEDS Olympus Rover Trials 2025–2026"
