---
title: "ClaudeTV: a $15 clock that shows your Claude usage"
description: "We reverse-engineered a GeekMagic SmallTV-Ultra, wrote custom ESP8266 firmware, and built a self-sustaining host service so a tiny desk display always shows our Claude session and weekly usage. Here's the build — including the silent-display saga and the token-refresh trick."
date: 2026-06-13
author: "Aditya Varma"
category: "Engineering"
tags: ["esp8266", "claude", "reverse-engineering", "hardware", "firmware"]
featured: true
image: "/blog/claudetv/hero.jpg"
imageAlt: "ClaudeTV: a small desk display showing Claude session and weekly usage percentages, weather, and a clock"
---

We run Claude Code all day. The one number we actually care about — *how much of the 5-hour
session and weekly quota is left* — lives behind a `/usage` command. We wanted it on the desk,
glanceable, always-on. So we bought a $15 WiFi clock, took its firmware apart, and replaced it.

This is **ClaudeTV**. It's open source: [github.com/latticelabs-au/ClaudeTV](https://github.com/latticelabs-au/ClaudeTV).

## The target

The [GeekMagic SmallTV-Ultra](https://www.aliexpress.com/item/1005007937948865.html) is a
cute little 240×240 IPS desk clock built on an **ESP8266** (an ESP-12F module, 4 MB flash, an
ST7789V panel). Stock, it shows weather and time. Crucially, its web updater at `/update` is a
plain unauthenticated `ESP8266HTTPUpdateServer` — it'll accept *any* valid ESP8266 image. That
means we could flash custom firmware **over WiFi, with no soldering**, and reflash the stock
firmware just as easily if we bricked the experience. Low stakes, high fun.

## Reverse-engineering the stock firmware

There's no source — just OTA blobs. So we pulled the firmware image apart with `esptool` and
`radare2`, carved out the `irom0` code segment, and read the strings. That gave us the entire
control surface for free: a tidy REST-ish API of `GET /*.json` readers and `GET /set?key=value`
writers, a `POST /doUpload` for images, and the list of built-in "themes." It also taught us
something useful: the stock build only *actually* fetches two remote things (weather and time).
The stock photo-frame and "monitor" themes we'd half-planned to hijack were dead ends. Better to
find that out from the disassembly than after a day of building on sand.

The conclusion from the RE work was, pleasingly, *don't keep reverse-engineering*. Several people
have already written open firmware for this exact panel, and the hardware (pinout, ST7789V driver,
PWM backlight on GPIO5) is well documented. The right move was to **write our own firmware** with
TFT_eSPI and own it end to end.

## Where the usage number actually comes from

Claude Code's `/usage` view is powered by a single endpoint: `GET /api/oauth/usage`, called with
your Claude OAuth token. It returns `five_hour` and `seven_day` utilization, each with a
`resets_at`. That's exactly the two numbers we want.

The ESP8266 can't (and shouldn't) hold your Claude credentials, so ClaudeTV is two parts:

- A small **host collector** (Python) that runs on any always-on box where Claude Code is logged
  in. It polls `/api/oauth/usage` and open-meteo for weather, and serves a compact JSON.
- The **firmware**, which just fetches that JSON over your LAN and draws it.

The collector **always serves the last-good value** and backs off on HTTP 429, so a rate-limit
blip never blanks the screen.

## The token keeps itself alive

The Claude access token is short-lived (~8 hours). If nothing refreshes it, the display goes
stale. The neat part: **Claude Code already knows how to refresh its own token** — it does it
whenever it runs. So the collector runs a tiny, near-free `claude -p "ping" --model haiku` before
the token expires, which makes Claude Code mint a fresh token and write it back. The collector
reads the token fresh each poll, so it picks up the refresh automatically. Self-sustaining, zero
babysitting. The master terminal shows token status and a manual "Refresh now" button just in case.

## The silent-display saga

Here's the bug we're proudest of fixing. Our first firmware *ticked* — a faint but maddening
electronic crackle, once per screen update. We chased it down the wrong trees first: it wasn't the
backlight (same tick at 0% and 100% brightness), it wasn't redraw size (a one-character clock tick
crackled just as loud), and it wasn't update frequency (the stock firmware redraws *every second*
in perfect silence).

The culprit was *how* we talked to the panel. The Adafruit ST7789 driver opens and closes a fresh
SPI transaction — toggling the chip-select line — around **every** drawing primitive. Each of those
CS start/stop envelopes is a tiny current transient that the panel's ceramics audibly tick on. The
stock firmware (and TFT_eSPI) hold **one** SPI transaction open and stream into it, CS low the whole
time. We switched to TFT_eSPI and called `startWrite()` once, never closing it. Silence. (We also
PWM the backlight at 22 kHz instead of holding it on full — DC-driving it overheats the boost
converter, which we discovered the honest way: by opening the case and feeling the inductor.)

## Designing for 240×240

The rest was iteration. We built a pixel-accurate browser emulator of the display so we could
screenshot and tweak the layout without reflashing — though the device's fonts render larger than
the browser's, which lied to us about overflow a few times. The final layout: two hero cards for
SESSION and WEEK percentages (green/amber/red by level) with reset times, a weather card that
cycles through now / feels-like / high / low / rain / humidity every few seconds, a big clock, and
the Lattice Labs mark. A web control panel handles brightness, an auto-dimming night mode, and OTA
updates; a host-side "master terminal" handles weather (search a city — it sets coordinates and
timezone for you), the token keeper, and service control.

## Try it

It's all on GitHub: firmware, the host collector + one-command systemd installer, the emulator, and
a prebuilt OTA image so you can flash without building. If you've got one of these clocks and you
live in Claude Code, it's a fun afternoon.

[github.com/latticelabs-au/ClaudeTV](https://github.com/latticelabs-au/ClaudeTV)
