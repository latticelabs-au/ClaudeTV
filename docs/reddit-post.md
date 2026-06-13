# Reddit post drafts

Pick a subreddit. r/esp8266 and r/diyelectronics want the hardware/RE angle; r/ClaudeAI wants
the "what is it" angle. Attach a photo of the device + maybe the master-terminal screenshot.

---

## r/esp8266 (or r/diyelectronics)

**Title:** Reverse-engineered a $15 GeekMagic clock and reflashed it to show my Claude AI usage (ESP8266 + TFT_eSPI)

**Body:**

I use Claude Code all day and wanted my session/weekly usage glanceable on the desk, so I took a
GeekMagic SmallTV-Ultra (ESP-12F, ST7789V 240×240) and wrote custom firmware for it.

Fun bits:

- The stock `/update` web endpoint is a plain ESP8266HTTPUpdateServer, so custom firmware flashes
  **over WiFi, no soldering**, and the stock firmware re-flashes the same way (fully reversible).
- Pulled the stock image apart with esptool/radare2 to map its little JSON API before deciding to
  just write my own with TFT_eSPI.
- Spent way too long on a faint **per-redraw "tick"** noise. Turned out the Adafruit driver toggles
  CS around every primitive; each SPI transaction start/stop is a current transient the panel ticks
  on. Holding one SPI transaction open (TFT_eSPI `startWrite()`, never closing) killed it. Stock
  firmware redraws every second in total silence for the same reason.
- Also caught myself DC-driving the backlight at 100% and cooking the boost-converter inductor —
  now it's PWM'd at 22 kHz.

Data comes from a tiny Python service on an always-on box (it polls Claude's usage endpoint and
serves the ESP a compact JSON), and it keeps its own auth token fresh automatically.

Open source (firmware + host service + a prebuilt OTA image): https://github.com/latticelabs-au/ClaudeTV

Happy to answer questions about the RE or the silent-SPI thing.

---

## r/ClaudeAI

**Title:** I built a little desk display that always shows my Claude session + weekly usage

**Body:**

Checking `/usage` got old, so I reflashed a $15 WiFi clock to show it permanently: 5-hour session %
and 7-day week % with reset times, plus weather and a clock.

A small service on my always-on machine reads the same usage endpoint the CLI uses and feeds the
display over my LAN. It refreshes its own token automatically (it nudges Claude Code to do it), so
it just runs.

It's open source if you want to build one — firmware, the host service with a one-command
installer, and a prebuilt image so you can flash without compiling:
https://github.com/latticelabs-au/ClaudeTV
