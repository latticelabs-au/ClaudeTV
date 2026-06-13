# ClaudeTV

Custom firmware + host service that turns a **GeekMagic SmallTV‑Ultra** (ESP8266 / ESP‑12F,
ST7789V 240×240) into a desk display for your **Claude subscription usage** — the same
5‑hour session % and 7‑day week % you see in Claude Code's `/usage`, with reset times —
alongside a cycling local‑weather turntable and a clock. Ships with a branded **master
terminal** for managing everything and an in‑browser **display emulator** for design work.

<p align="center"><em>SESSION 7% · WEEK 2% · Melbourne 16°C · live clock · Lattice Labs mark</em></p>

---

## How it works

```
 Claude Code (on an always-on host)              ESP8266 (SmallTV-Ultra)
        │  keeps OAuth token fresh                       │
        ▼                                                ▼
  ~/.claude/.credentials.json                      ┌───────────────┐
        │                                           │  ClaudeTV fw  │
   collector ──► api.anthropic.com/api/oauth/usage  │  GET /usage   │◄── LAN
   (Python)  ──► open-meteo.com (weather, no key)   └───────────────┘
        │                                                ▲
   GET /usage ──────────────────────────────────────────┘
   GET /  (master terminal: status, config, token keeper, service control)
```

- **Collector** (`host/claude_usage_server.py`) polls Anthropic's `/api/oauth/usage` (the exact
  endpoint Claude Code's `/usage` view uses) with your local Claude OAuth token, plus keyless
  weather from open‑meteo. It **always serves the last‑good value** and backs off on HTTP 429, so a
  rate‑limit blip never blanks the screen.
- **Token keeper** (built into the collector): the Claude access token is short‑lived (~8 h). The
  keeper runs `claude -p "ping" --model haiku` before expiry, which makes **Claude Code refresh its
  own token** and write it back to the credentials file. The collector reads the token fresh each
  poll, so it always picks up the refresh. (Negligible usage cost.)
- **Firmware** (`firmware/claudetv/`) fetches the collector JSON over your LAN and renders it.
  Rendering uses **TFT_eSPI with one held‑open SPI transaction** (CS stays low continuously, like the
  stock firmware) so there is **no per‑redraw coil/cap tick**.

The OAuth token is **never logged, shown, or sent anywhere except `api.anthropic.com`**.

---

## Prerequisites

**On the host that runs the collector** (any always‑on box on your LAN — a NAS VM, a Pi, etc.):

1. **Python 3.9+**
2. **Claude Code installed *and logged in*** (`claude` on the box, `claude /login` done).
   This is required — Claude Code is what refreshes the OAuth token the collector reads. If Claude
   Code is not logged in, the token will eventually expire and usage will stop updating. The master
   terminal shows token status (`valid` / `expired`) and a **Refresh now** button.
3. The host must be reachable from the device's network on the collector port (default **8088**).

**For building/flashing firmware:** Arduino IDE or `arduino-cli` with the ESP8266 core, plus the
**TFT_eSPI** and **ArduinoJson** libraries. (First flash to a stock GeekMagic device is done over
the air — no serial adapter needed.)

---

## Setup

### 1. Host collector + master terminal
```bash
# on the always-on host (Claude Code installed + logged in):
git clone https://github.com/latticelabs-au/ClaudeTV.git && cd ClaudeTV/host
cp .env.example .env                 # optional — or just edit live in the terminal
sudo bash install.sh                 # installs + starts the systemd service
# open the master terminal:
xdg-open http://<host-ip>:8088/
```
`install.sh` creates a `systemd` unit (auto‑start on boot, auto‑restart). The **master terminal**
(`http://<host-ip>:8088/`) shows live status and lets you edit weather/Claude config, watch the
token keeper, refresh the token, and restart the service — no SSH needed.

### 2. Firmware
1. `cp firmware/claudetv/config.h.example firmware/claudetv/config.h` and fill in your WiFi +
   the collector URL (`http://<host-ip>:8088/usage`).
2. Copy `firmware/User_Setup.h` into your TFT_eSPI library folder (overwrites its `User_Setup.h`).
3. Build (board = generic ESP8266, 4 MB, FS 1 MB):
   ```bash
   arduino-cli compile --fqbn esp8266:esp8266:generic:eesz=4M1M --output-dir build firmware/claudetv
   ```
4. **Flash over the air** to the stock device's web updater (no serial needed):
   ```bash
   curl -F "firmware=@build/claudetv.ino.bin" http://<device-ip>/update
   ```
   After it reboots, the device is at `http://claudetv.local/` (mDNS) with its own control panel and
   OTA. To revert, flash the stock GeekMagic firmware the same way (its saved settings survive — OTA
   only replaces the app, not the SPIFFS partition).

---

## Features

**Display:** two‑column SESSION / WEEK hero cards with reset times · cycling weather turntable
(now / feels‑like / high / low / rain % / humidity) · big clock · auto night‑dim · Lattice Labs logo.

**Device control panel** (`http://claudetv.local/`): brightness, night mode (default **30 % from
21:00–07:00**, fully configurable), flip display 180°, refresh interval, reboot, firmware OTA.

**Master terminal** (`http://<host-ip>:8088/`): live service/token/usage/weather status, weather
config CRUD (city, lat/lon, poll interval, timezone), Claude config (credentials path, claude
binary, ping model, refresh margin), token keeper with **Refresh now**, restart service.

**Emulator** (`emulator/index.html`): a pixel‑accurate 240×240 canvas mirror of the firmware layout
that pulls live data from the collector — for iterating the design in a browser before flashing.

---

## Hardware notes (ESP‑12F / ST7789V 240×240)

The device: **GeekMagic SmallTV‑Ultra** (ESP8266 variant) —
[AliExpress](https://www.aliexpress.com/item/1005007937948865.html). ~$15, ships with stock
firmware; ClaudeTV flashes over the air, no soldering.

| Signal | GPIO | | Signal | GPIO |
|---|---|---|---|---|
| MOSI | 13 | | CS | 15 |
| SCLK | 14 | | DC | 0 |
| RST | 2 | | Backlight | 5 (active‑low PWM) |

The stock web updater (`/update`) is a standard `ESP8266HTTPUpdateServer` — it accepts custom
images, which is why first flash works over the air. Backlight is PWM'd at ~22 kHz; never hold it
steady full‑on (it overdrives/overheats the backlight boost converter).

---

## Repo layout

```
firmware/claudetv/   ClaudeTV firmware (.ino, config.h.example, logo.h)
firmware/User_Setup.h TFT_eSPI board config for this panel
host/                collector + master terminal + systemd installer
emulator/            in-browser 240x240 display emulator
```

## License

MIT.
