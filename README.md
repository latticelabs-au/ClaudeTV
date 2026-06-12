# ClaudeTV

Custom firmware that turns a **GeekMagic SmallTV-Ultra** (ESP8266 / ESP-12F, ST7789V 240×240)
into a desk display for your **Claude subscription usage** — 5-hour session % and 7-day week %
with reset times — alongside local weather and a clock, with a full web control panel.

![screen: CLAUDE USAGE / SESSION % / WEEK % / weather / clock]

## How it works

```
Claude OAuth token (~/.claude)                 ESP8266 (SmallTV-Ultra)
        │                                              │
  api.anthropic.com/api/oauth/usage   ──►  host collector  ──►  GET /usage  ──►  display
  open-meteo.com (weather, no key)         (Python, systemd)      (LAN)
```

- **Host collector** (`host/claude_usage_server.py`): polls Anthropic's `/api/oauth/usage`
  (the same endpoint the Claude Code `/usage` view uses) with your local Claude OAuth token,
  plus keyless weather from open-meteo. It **always serves the last-good value** and backs off
  on HTTP 429, so a rate-limit blip never blanks the screen. The token is read fresh from
  `~/.claude/.credentials.json` each poll and is never logged or sent anywhere but Anthropic.
- **Firmware** (`firmware/claudetv/`): fetches the collector JSON over your LAN and renders it.

## Display

`SESSION  xx%  resets <time>` · `WEEK  xx%  resets <date time>` · weather (city/temp/condition) · clock.

Rendering uses **TFT_eSPI with a single held-open SPI transaction** (CS stays low continuously,
like the stock firmware) so there is **no per-redraw coil/cap tick**.

## Web control panel (`http://<device-ip>/`)

Live status + **brightness**, **night mode** (auto-dim schedule), **flip display**, **refresh
interval**, **reboot**, and **firmware OTA**. Settings persist in EEPROM.

## Setup

### 1. Host collector (any always-on box that has Claude Code logged in)
```bash
# needs python3; reads ~/.claude/.credentials.json on that box
sudo cp host/claude-usage.service /etc/systemd/system/
cp host/claude_usage_server.py ~/claude_usage_server.py   # match WorkingDirectory/User in the unit
sudo systemctl enable --now claude-usage.service
curl localhost:8088/usage
```

### 2. Firmware
1. `cp firmware/claudetv/config.h.example firmware/claudetv/config.h` and fill in WiFi + the
   collector URL (`http://<host-ip>:8088/usage`).
2. Copy `firmware/User_Setup.h` into your TFT_eSPI library folder (overwrites its `User_Setup.h`).
3. Build (Arduino IDE or arduino-cli, ESP8266 core), board = generic ESP8266, 4 MB (FS 1 MB):
   ```bash
   arduino-cli compile --fqbn esp8266:esp8266:generic:eesz=4M1M --output-dir build firmware/claudetv
   ```
4. **Flash over the air** to the stock device's updater (no serial needed):
   ```bash
   curl -F "firmware=@build/claudetv.ino.bin" http://<device-ip>/update
   ```
   The stock `/update` is a standard `ESP8266HTTPUpdateServer`. To revert, flash the stock
   firmware from the GeekMagic repo the same way (its SPIFFS settings are preserved).

## Hardware notes (ESP-12F / ST7789V 240×240)

| Signal | GPIO | | Signal | GPIO |
|---|---|---|---|---|
| MOSI | 13 | | CS | 15 |
| SCLK | 14 | | DC | 0 |
| RST | 2 | | Backlight | 5 (active-low PWM) |

Backlight is PWM'd at ~22 kHz — never hold it steady full-on (it overdrives/overheats the
backlight boost converter).

## License

MIT.
