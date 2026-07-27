# ORT Rover — QMSEDS / Queen Mary University of London
**Competition:** UKSEDS Olympus Rover Trials 2025–2026

---

## Hardware List

| Component | Description |
|---|---|
| Raspberry Pi  | Rover onboard computer |
| 2× Cytron MDD10A | Dual-channel motor driver (one per side) |
| MG92B servo | Camera pitch control |
| ADS1115 | 12-bit ADC over I2C for battery voltage measurement |
| IR obstacle sensors (×2) | Digital active-low sensors: front and rear |
| WiFi router | Network bridge between rover and base station |

---

## Wiring Summary

GPIO pin assignments are defined in [rover/config.py](rover/config.py). Key connections:

**MDD10A Driver 1 — Left side:**
- Left front: PWM → GPIO 12, DIR → GPIO 6
- Left rear:  PWM → GPIO 13, DIR → GPIO 5

**MDD10A Driver 2 — Right side:**
- Right front: PWM → GPIO 18, DIR → GPIO 24
- Right rear:  PWM → GPIO 19, DIR → GPIO 25

**Camera servo (MG92B):** GPIO 17

**IR sensors (active-low — LOW = obstacle):**
- Front: GPIO 23
- Rear:  GPIO 22

**ADS1115 (I2C):** default address 0x48, battery on channel 0.
Wire a resistor voltage divider (R1=10kΩ, R2=3.3kΩ) between the battery and ADS1115 channel 0.
Adjust `BATTERY_DIVIDER_R1` / `BATTERY_DIVIDER_R2` in `rover/config.py` to match your circuit.

---

## Pi Setup

```bash
# 1. Clone the repo
git clone <repo-url> ~/ort-rover
cd ~/ort-rover/rover

# 2. Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Edit rover/config.py
#    Set MOCK_MODE = False
#    Set BASE_STATION_IP to the laptop's IP on your router
```

---

## Base Station Setup

```bash
# 1. Navigate to the base station folder
cd ~/ort-rover/base_station

# 2. Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Edit base_station/config.py
#    Set ROVER_IP to the Pi's IP on your router
```

---

## How to Run

**Step 1 — Start the rover (Pi):**
```bash
cd ~/ort-rover/rover
bash start_rover.sh
```

**Step 2 — Start the base station (laptop), after rover is running:**
```bash
cd ~/ort-rover
source venv/bin/activate
python base_station/main.py
```

Optional flags:
```
--rover-ip <IP>      override ROVER_IP from config
--mission-id <ID>    override auto-generated mission folder name
--fullscreen         launch pygame fullscreen
--no-gamepad         disable gamepad detection
--mock               show simulation banner on video panel
```

---

## Controls

| Key / Input | Action |
|---|---|
| W / ↑ | Forward |
| S / ↓ | Reverse |
| A / ← | Turn left |
| D / → | Turn right |
| Q | Servo tilt up |
| E | Servo tilt down |
| X | E-Stop (latching) |
| R | Release E-Stop |
| SPACE | Manual QR capture |
| TAB | Switch tabs |
| ESC | Quit |

Gamepad: left stick = drive, A/Cross = E-Stop, B/Circle = release, Y/Triangle = capture, L1/R1 = servo tilt.

---

## How to End a Mission

1. Click the **Export** button in the base station UI (bottom-right, visible on both tabs).
2. Mission data is saved to `missions/<mission_id>/`:
   - `submission_package.json` — structured results for judges
   - `captures/*.jpg` — all captured still images
3. Hand the `missions/<mission_id>/` folder to the judges (USB or direct copy).

On quit, an automatic export also runs so data is never lost.

---

## Network Configuration

Assign static IPs on your router:
- Rover Pi: `192.168.1.100`
- Operator laptop: `192.168.1.50`

Update `ROVER_IP` in `base_station/config.py` and `BASE_STATION_IP` in `rover/config.py` if you use different addresses.

ZeroMQ ports (all TCP):
- 5556 — commands (base → rover)
- 5557 — telemetry (rover → base)
- 5558 — video stream (rover → base)
