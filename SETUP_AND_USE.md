# ORT Rover — Setup, Usage & Testing Guide
**QMSEDS / Queen Mary University of London**

---

## Contents
1. [Raspberry Pi setup](#1-raspberry-pi-setup)
2. [Network setup](#2-network-setup)
3. [Running the system](#3-running-the-system)
4. [Controls reference](#4-controls-reference)
5. [Ending a mission and exporting](#5-ending-a-mission-and-exporting)
6. [Testing — what to run and what to look for](#6-testing)

---

## 1. Raspberry Pi setup

Do this over SSH before the competition, not on the day.

### 1.1 Install system packages

```bash
sudo apt update
sudo apt install -y pigpio python3-pip python3-venv libzbar0
```

### 1.2 Install Python dependencies

```bash
cd ~/ort-rover/rover
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 1.3 Configure

Open `rover/config.py` and set:

```python
MOCK_MODE        = False          # ← must be False on the real Pi
BASE_STATION_IP  = "192.168.1.50" # ← laptop's IP on your router
```

Verify all GPIO pin numbers match your wiring (see `rover/config.py` comments and `README.md` wiring table).

Verify the voltage divider values match your battery measurement circuit:

```python
BATTERY_DIVIDER_R1 = 10000   # ← your R1 in ohms
BATTERY_DIVIDER_R2 = 3300    # ← your R2 in ohms
```

### 1.4 Test mock mode on the Pi first

Before connecting any hardware, run with `MOCK_MODE = True` to confirm Python and ZeroMQ are working:

```bash
cd ~/ort-rover/rover
source venv/bin/activate
python rover.py
```

Expected output:
```
[gpio       ] INFO  MOCK_MODE — hardware calls disabled
[motors     ] INFO  ready
[servo      ] INFO  ready at 1500 µs
[ir         ] INFO  ready
[battery    ] INFO  ready (mock)
[comms      ] INFO  bound — cmd:5556  telem:5557  video:5558
[rover      ] INFO  all subsystems ready. LOG_DIR=mission_logs/...
[rover      ] INFO  entering main event loop
```

After 2 seconds with no base station heartbeat you will see:
```
[watchdog   ] WARNING  heartbeat timeout — entering safe state
```
This is **correct** — it confirms the watchdog works. Press **Ctrl-C** to stop.

### 1.5 Set MOCK_MODE = False and test with real hardware

```python
MOCK_MODE = False
```

Run `python rover.py` and check the log output confirms each subsystem initialises without error. If a subsystem fails (e.g. camera not found, pigpio not running) it will log an error.

If pigpio is not running:
```bash
sudo pigpiod
```

### 1.6 Auto-start on boot (optional but recommended for competition day)

Copy the service file and enable it:

```bash
sudo cp ~/ort-rover/rover/rover.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rover.service
sudo systemctl start rover.service
```

Check it started:
```bash
sudo systemctl status rover.service
```

To disable auto-start: `sudo systemctl disable rover.service`

---

## 2. Network setup

### 2.1 Assign static IPs on your router

| Device | IP |
|---|---|
| Raspberry Pi | `192.168.1.100` |
| Operator laptop | `192.168.1.50` |

Set these via DHCP reservation (MAC address binding) on your router — do not rely on dynamic IPs on competition day.

### 2.2 Update config files to match

`rover/config.py` → `BASE_STATION_IP = "192.168.1.50"`
`base_station/config.py` → `ROVER_IP = "192.168.1.100"`

### 2.3 Verify connectivity

From the laptop:
```bash
ping 192.168.1.100    # should respond from Pi
```

From the Pi:
```bash
ping 192.168.1.50     # should respond from laptop
```

If ping works but the base station still shows LINK LOST, check that no firewall is blocking TCP ports 5556, 5557, 5558.

### 2.4 Ethernet fallback

If WiFi drops during a run, plug an Ethernet cable directly between the laptop and the Pi (or into the router). The base station detects this automatically and shows an **"Ethernet fallback available"** alert.

---

## 3. Running the system

### Boot sequence — follow this order every time

**Step 1 — Power on the Pi.** If auto-start is enabled, rover.py starts automatically. Otherwise SSH in and run:

```bash
cd ~/ort-rover/rover
bash start_rover.sh
```

**Step 2 — Confirm the rover is ready.** Check the log (via SSH or serial) for:
```
[rover] INFO  all subsystems ready
```

**Step 3 — Start the base station on the laptop:**

```bash
cd ~/ort-rover
bash base_station/start_base.sh
```

Or manually:
```bash
source venv/bin/activate
python base_station/main.py
```

**Step 4 — Wait for link.** Within a few seconds the UI should show:
- **LINK ● ONLINE** (green) in the telemetry panel
- Live video in the video panel
- Battery voltage and CPU temperature updating

If LINK LOST persists after 10 seconds, check the network (see Section 4).

**Step 5 — Quick pre-drive check** (30 seconds):
1. Press **W** — rover should move forward
2. Press **S** — rover should reverse
3. Press **A** / **D** — left / right turns
4. Press **X** — E-STOP ACTIVE alert appears, W does nothing
5. Press **R** — alert clears, W works again
6. Press **Q** / **E** — camera tilts up / down

---

## 4. Controls reference

### Keyboard

| Key | Action |
|---|---|
| **W** / **↑** | Forward |
| **S** / **↓** | Reverse |
| **A** / **←** | Turn left |
| **D** / **→** | Turn right |
| **Q** | Camera tilt up |
| **E** | Camera tilt down |
| **X** | E-Stop (latching — rover stops and ignores drive commands) |
| **R** | Release E-Stop |
| **SPACE** | Manual QR capture (saves still + logs to Tab 2) |
| **TAB** | Switch between DRIVE tab and QR LOG tab |
| **ESC** | Quit (triggers auto-export) |

### Gamepad (Xbox / PlayStation layout)

| Input | Action |
|---|---|
| Left stick | Throttle (Y axis) + steering (X axis) |
| A / Cross | E-Stop |
| B / Circle | Release E-Stop |
| Y / Triangle | Manual capture |
| L1 | Camera tilt down |
| R1 | Camera tilt up |

### Telemetry indicators

| Indicator | Green | Amber | Red |
|---|---|---|---|
| Battery | ≥ 7.0 V | 6.6 – 7.0 V | < 6.6 V |
| CPU temp | < 60 °C | 60 – 75 °C | ≥ 75 °C |
| IR sensors | Clear | — | Obstacle |
| Rover state | OPERATING | IDLE | SAFE STATE / ERROR |
| Link | ONLINE | — | LOST |

---

## 5. Ending a mission and exporting

### During the mission

Tab 2 (QR LOG) shows every captured QR code with timestamp and thumbnail. The base station auto-captures when a QR code scores above the quality threshold — you do not need to press SPACE unless you want to force a capture.

### At the end

1. Click the **EXPORT** button (bottom-right, visible on both tabs).
2. The button shows **EXPORTED ✓** for 3 seconds and displays the save path.
3. Files are written to `missions/<mission_id>/`:
   - `submission_package.json` — structured results for the judges
   - `captures/*.jpg` — all still images
   - `mission_log.json` — full event log including ESTOP, LINK events

On **ESC** (quit), a second automatic export runs as a safety net.

### Handing over to judges

Copy the entire `missions/<mission_id>/` folder to USB or share directly. The `submission_package.json` file is the primary deliverable.

---

## 6. Testing

Run all tests from the project root with the base station venv active.

```bash
cd ~/ort-rover
source venv/bin/activate
```

---

### 6.1 Unit tests (no hardware, no rover needed)

These run on any machine in under 10 seconds.

```bash
pytest tests/test_comms.py tests/test_logger.py tests/test_qr_pipeline.py -v
```

**Expected result:** 54 passed

What each file covers:

| File | Tests |
|---|---|
| `test_comms.py` | Diff-drive maths, speed clamping, E-Stop flag, message JSON format |
| `test_logger.py` | Mission directory creation, QR capture logging, deduplication, atomic writes, thread safety |
| `test_qr_pipeline.py` | QR quality scoring, still image saving, auto-capture trigger, cooldown, manual capture |

If any test fails, **do not proceed to competition** until it is fixed.

---

### 6.2 Mock integration test (laptop only, no rover needed)

This test runs the full comms stack end-to-end using a fake rover.

**Terminal 1 — start the mock rover:**

```bash
python tests/mock_rover.py
```

Expected output:
```
Mock rover bound — cmd:5556  telem:5557  video:5558
  Ctrl-C to stop
```

**Terminal 2 — start the base station:**

```bash
python base_station/main.py --rover-ip 127.0.0.1 --mock
```

**What to verify:**

| Check | Pass condition |
|---|---|
| Video panel | Shows "MOCK ROVER" frame with timestamp overlay |
| LINK status | Shows ONLINE (green) within 5 seconds |
| Battery | Shows ~8.4 V, slowly decreasing |
| Uptime | Counting up in real time |
| CPU temp | Shows ~45–70 °C |
| Drive commands | Pressing W/A/S/D prints `[CMD] {"type": "drive", ...}` in Terminal 1 |
| Servo | Pressing Q/E prints `[CMD] {"type": "servo", ...}` in Terminal 1 |
| E-Stop | Press X → alert appears, W prints nothing in Terminal 1; press R → W works again |
| QR auto-capture | Wait ~30 frames — QR code appears in video; capture fires; entry appears in Tab 2 |
| Manual capture | Press SPACE → entry with `manual_noqr` appears in Tab 2 |
| Export | Click Export → `missions/<id>/submission_package.json` created |

---

### 6.3 Link-drop simulation test

In Terminal 1, pass `--link-drop-at 15`:

```bash
python tests/mock_rover.py --link-drop-at 15
```

Start the base station normally (Terminal 2).

| Time | Expected |
|---|---|
| t=0–14 s | LINK ONLINE, telemetry updating normally |
| t=15 s | LINK LOST alert appears in base station UI |
| t=15–20 s | Battery and uptime freeze (no new telemetry) |
| t=20 s | LINK LOST clears, telemetry resumes |

After quitting, open `missions/<id>/mission_log.json` and confirm it contains `LINK_LOST` and `LINK_RESTORED` action log entries.

---

### 6.4 Real hardware smoke test (on Pi with MOCK_MODE = False)

Run after all software is installed and wired up, before competition day.

```bash
# On Pi — start rover with real hardware
cd ~/ort-rover/rover
source venv/bin/activate
python rover.py
```

```bash
# On laptop — connect to Pi
python base_station/main.py --rover-ip 192.168.1.100
```

| Check | Method | Pass condition |
|---|---|---|
| All subsystems start | Pi log output | No ERROR lines |
| Video stream | UI | Real camera image appears |
| Battery voltage | UI | Reads correct pack voltage (e.g. 7.4–8.4 V for 2S LiPo) |
| CPU temperature | UI | Reads ~40–55 °C at rest |
| Forward drive | Press W | Rover moves forward, all four wheels turn |
| Reverse drive | Press S | Rover reverses |
| Left turn | Press A | Rover turns left |
| Right turn | Press D | Rover turns right |
| Camera tilt | Press Q / E | Servo moves visibly |
| IR sensors | Place hand in front of sensor | IR dot turns red in UI |
| Obstacle blocking | IR triggered + press W | Rover does not move (OBSTACLE_BLOCK = True) |
| E-Stop | Press X | Rover stops immediately, W ignored |
| E-Stop release | Press R | W works again |
| QR capture | Hold QR code in front of camera | Auto-capture fires, appears in Tab 2 |
| Heartbeat watchdog | Kill rover.py on Pi (Ctrl-C) | Base station shows LINK LOST; rover motors stop |
| Watchdog restore | Restart rover.py | LINK LOST clears |
| Export | Click Export | Files saved to `missions/<id>/` |

---

### 6.5 Pre-competition day checklist

Complete these the evening before, in order:

- [ ] Run unit tests: `pytest tests/test_comms.py tests/test_logger.py tests/test_qr_pipeline.py` → 54 passed
- [ ] Run mock integration test (Section 8.2) — all checks green
- [ ] Run link-drop test (Section 8.3) — LINK_LOST and LINK_RESTORED in log
- [ ] Charge battery fully
- [ ] Set `MOCK_MODE = False` in `rover/config.py`
- [ ] Confirm `BASE_STATION_IP` and `ROVER_IP` are correct in both config files
- [ ] Confirm static IPs are set on the router
- [ ] Run real hardware smoke test (Section 8.4) — all checks green
- [ ] Do a full end-to-end run: drive around, capture at least 2 different QR codes, click Export, open `submission_package.json` and confirm it has the correct data
- [ ] Confirm Ethernet cable is packed as fallback

---

### 6.6 Competition day startup (target: 30 seconds)

```
1. Power on rover Pi
2. Wait 10 seconds for auto-start
3. Open laptop, run: bash base_station/start_base.sh
4. Confirm LINK ONLINE and live video within 10 seconds
5. Quick W/S/A/D/X/R check — 10 seconds
6. Hold test QR in front of camera — confirm capture appears in Tab 2
7. Mission begins
```

If something is wrong during this sequence, see the troubleshooting notes below.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| LINK LOST at startup | Wrong IP in config, or Pi not booted yet | Check `ROVER_IP` in base_station/config.py; wait 15 s and retry |
| LINK LOST at startup | pigpiod not running | SSH to Pi: `sudo pigpiod` |
| No video, telemetry OK | Camera not connected or wrong index | Check `CAMERA_INDEX` in rover/config.py |
| Video freezes but telemetry fine | Network congestion — reduce JPEG quality | Lower `JPEG_QUALITY` in rover/config.py (try 60) |
| Battery reads 0.0 V | ADS1115 not connected or wrong I2C address | Check wiring; verify `BATTERY_I2C_ADDRESS = 0x48` in rover/config.py |
| Rover doesn't move | E-Stop may be active | Press R to release; check UI for E-STOP alert |
| Rover doesn't move | OBSTACLE_BLOCK = True and IR triggered | Move obstacle or press Obstacle Block toggle in UI |
| QR never auto-captures | Camera too far from QR, or blurry | Move closer; check that `QR_QUALITY_THRESHOLD` (0.55) is appropriate |
| Export button does nothing | No mission started yet | Check `missions/` directory; check `session.log` for errors |
