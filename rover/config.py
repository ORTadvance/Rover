# ── Network ────────────────────────────────────────────
BASE_STATION_IP   = "192.168.1.50"   # laptop IP on your router
CMD_SUB_PORT      = 5556             # rover SUBs commands here
TELEM_PUSH_PORT   = 5557             # rover PUSHes telemetry here
VIDEO_PUSH_PORT   = 5558             # rover PUSHes video here

# ── Heartbeat ──────────────────────────────────────────
HEARTBEAT_TIMEOUT_S  = 2.0   # seconds without heartbeat → safe state
HEARTBEAT_SEND_HZ    = 2     # how often rover sends its own heartbeat

# ── Motor GPIO pins (MDD10A Driver 1 — left side) ──────
LEFT_FRONT_PWM  = 12
LEFT_FRONT_DIR  = 6
LEFT_REAR_PWM   = 13
LEFT_REAR_DIR   = 5

# ── Motor GPIO pins (MDD10A Driver 2 — right side) ─────
RIGHT_FRONT_PWM = 18
RIGHT_FRONT_DIR = 24
RIGHT_REAR_PWM  = 19
RIGHT_REAR_DIR  = 25

# ── Motor behaviour ────────────────────────────────────
PWM_FREQUENCY   = 1000   # Hz
MAX_SPEED       = 1.0    # normalised cap [0.0, 1.0]

# ── Motor soft-start / ramp ────────────────────────────
RAMP_ENABLED    = True   # True = ease motor speed toward target (soft-start)
                         # False = apply commanded speed instantly (old behaviour)
RAMP_RATE       = 2.5    # max change in normalised speed per second
                         # 2.5 → stop-to-full in ~0.4 s; lower = gentler, softer on the battery
RAMP_HZ         = 50     # how often the ramp loop updates the motors

# ── Camera servo (MG92B) ───────────────────────────────
SERVO_PIN       = 17
SERVO_MIN_PW    = 500    # microseconds — full down
SERVO_MID_PW    = 1500   # neutral / centre
SERVO_MAX_PW    = 2500   # full up
SERVO_STEP_PW   = 100    # pulse width change per key press
                         # increase for faster tilt, decrease for finer control

# ── IR obstacle sensors (active-low: LOW = obstacle) ───
IR_FRONT_PIN    = 23
IR_REAR_PIN     = 22

# ── Battery monitor (ADS1115 over I2C) ─────────────────
BATTERY_I2C_ADDRESS  = 0x48   # default ADS1115 address
BATTERY_CHANNEL      = 0      # ADS1115 channel (0–3)
BATTERY_DIVIDER_R1   = 10000  # voltage divider R1 in ohms — adjust to your circuit
BATTERY_DIVIDER_R2   = 3300   # voltage divider R2 in ohms — adjust to your circuit
BATTERY_WARN_V       = 7.0    # amber warning threshold
BATTERY_CRIT_V       = 6.6    # red critical threshold

# ── Camera / vision ────────────────────────────────────
CAMERA_INDEX         = 0
FRAME_WIDTH          = 640
FRAME_HEIGHT         = 480
FRAME_RATE           = 30     # target FPS for video stream
JPEG_QUALITY         = 80     # 0–100, lower = smaller packet, higher = sharper
QR_BLUR_THRESHOLD    = 100.0  # Laplacian variance — frames below this are too blurry for QR

# ── Obstacle safety ────────────────────────────────────
OBSTACLE_BLOCK_DRIVE = False  # IR obstacle blocking disabled (sensors removed)
                              # set True to re-enable drive blocking from the IR sensors

# ── Mock mode ──────────────────────────────────────────
MOCK_MODE = False   # True = no GPIO, no camera, runs on any PC
                   # SET TO FALSE before deploying to the real Pi
