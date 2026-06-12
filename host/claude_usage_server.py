#!/usr/bin/env python
"""
Claude usage + weather collector for the ClaudeTV SmallTV display.

- Polls Anthropic GET /api/oauth/usage with the local Claude Code OAuth token
  (the same endpoint the CLI /usage view uses) for session(5h)/week(7d) utilization.
- Polls open-meteo (no API key) for local weather.
- ALWAYS serves the last-good usage so a transient 429/error never blanks the display.
- Backs off on HTTP 429 (respects Retry-After) so we stop hammering the rate limit.

Token is read fresh from ~/.claude/.credentials.json each poll (tracks CLI refreshes)
and is never logged or sent anywhere except api.anthropic.com.

Run: python3 claude_usage_server.py   (serves 0.0.0.0:8088, ESP -> /usage)
"""
import json, os, time, threading, urllib.request, urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Australia/Melbourne")
except Exception:
    LOCAL_TZ = None

# ---- config ----
CRED          = os.path.expanduser("~/.claude/.credentials.json")
USAGE_URL     = "https://api.anthropic.com/api/oauth/usage"
PORT          = 8088
USAGE_EVERY   = 150          # base seconds between usage polls (gentle -> avoid 429)
WEATHER_EVERY = 900          # seconds between weather polls
LAT, LON, CITY = -37.8136, 144.9631, "Melbourne"
WX = "https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m,weather_code&timezone=auto" % (LAT, LON)

WMO = {0:"Clear",1:"Clear",2:"Cloudy",3:"Overcast",45:"Fog",48:"Fog",
       51:"Drizzle",53:"Drizzle",55:"Drizzle",61:"Rain",63:"Rain",65:"Heavy rain",
       66:"Rain",67:"Rain",71:"Snow",73:"Snow",75:"Snow",77:"Snow",
       80:"Showers",81:"Showers",82:"Showers",85:"Snow",86:"Snow",
       95:"Storm",96:"Storm",99:"Storm"}

_lock = threading.Lock()
_usage = None            # last-good usage dict
_usage_ts = 0
_usage_err = "starting"
_wx = None               # last-good weather dict


def _token():
    return json.load(open(CRED, encoding="utf-8"))["claudeAiOauth"]["accessToken"]

def _clock(dt):
    h = dt.hour % 12 or 12
    return "%d:%02d%s" % (h, dt.minute, "am" if dt.hour < 12 else "pm")

def _parse(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ) if LOCAL_TZ else dt.astimezone()

def fetch_usage():
    """Returns parsed usage dict, or raises (with .code on HTTPError)."""
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + _token(),
        "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode())
    fh, sw = data.get("five_hour") or {}, data.get("seven_day") or {}
    out = {"s": round(float(fh.get("utilization", 0))),
           "w": round(float(sw.get("utilization", 0))), "sr": "", "wr": ""}
    if fh.get("resets_at"):
        out["sr"] = _clock(_parse(fh["resets_at"]))
    if sw.get("resets_at"):
        d = _parse(sw["resets_at"]); out["wr"] = "%s %d %s" % (d.strftime("%b"), d.day, _clock(d))
    return out

def fetch_weather():
    with urllib.request.urlopen(WX, timeout=8) as r:
        c = json.loads(r.read().decode()).get("current", {})
    return {"wt": round(float(c.get("temperature_2m", 0))),
            "wc": WMO.get(int(c.get("weather_code", -1)), "--"), "city": CITY}


def poller():
    global _usage, _usage_ts, _usage_err, _wx
    next_usage = 0.0; backoff = USAGE_EVERY; next_wx = 0.0
    while True:
        now = time.time()
        if now >= next_usage:
            try:
                u = fetch_usage()
                with _lock: _usage = u; _usage_ts = int(now); _usage_err = ""
                backoff = USAGE_EVERY; next_usage = now + USAGE_EVERY
                print("[%s] usage s=%d%% w=%d%% sr=%s wr=%s" % (time.strftime("%H:%M:%S"), u["s"], u["w"], u["sr"] or "-", u["wr"] or "-"))
            except urllib.error.HTTPError as e:
                ra = e.headers.get("Retry-After")
                wait = int(ra) if (ra and ra.isdigit()) else min(backoff * 2, 600)
                backoff = wait; next_usage = now + wait
                with _lock: _usage_err = "http %d" % e.code
                print("[%s] usage HTTP %d -> backoff %ds (last-good retained)" % (time.strftime("%H:%M:%S"), e.code, wait))
            except Exception as e:
                next_usage = now + 30
                with _lock: _usage_err = str(e)[:50]
                print("[%s] usage err: %s" % (time.strftime("%H:%M:%S"), e))
        if now >= next_wx:
            try:
                with _lock: _wx = fetch_weather()
                next_wx = now + WEATHER_EVERY
            except Exception as e:
                next_wx = now + 120
                print("[%s] weather err: %s" % (time.strftime("%H:%M:%S"), e))
        time.sleep(2)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        with _lock:
            u, ts, err, wx = _usage, _usage_ts, _usage_err, _wx
        st = {"ok": 1 if u else 0, "age": (int(time.time()) - ts) if ts else -1, "err": err}
        if u: st.update(u)
        else: st.update({"s": 0, "w": 0, "sr": "", "wr": ""})
        if wx: st.update(wx)
        body = json.dumps(st, separators=(",", ":")).encode()
        if self.path.startswith("/usage"):
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            h = ("<h2>ClaudeTV collector</h2><pre>%s</pre>" % json.dumps(st, indent=2)).encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(h)


if __name__ == "__main__":
    threading.Thread(target=poller, daemon=True).start()
    print("ClaudeTV collector on http://0.0.0.0:%d  (ESP -> /usage)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
