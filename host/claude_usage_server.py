#!/usr/bin/env python
"""
ClaudeTV collector + Master Terminal.

- Serves the ESP display its data at  GET /usage
- Serves a branded management terminal at  GET /  (live status, weather/Claude config CRUD,
  token-keeper status, service control)
- Polls Anthropic /api/oauth/usage (the Claude Code /usage endpoint) for session/week % plus
  the model-scoped weekly limit (e.g. Fable) out of limits[], and open-meteo (no key) for
  weather. Always serves last-good; backs off on HTTP 429.

TOKEN KEEPER: the Claude OAuth access token is short-lived (~8h) but comes with a refresh
token whose ~28-day validity window ROLLS FORWARD on every refresh, so one login lasts
indefinitely as long as the keeper refreshes at least monthly (it refreshes every ~8h). The
keeper speaks the OAuth refresh grant natively (the same public-client token endpoint Claude
Code uses) and writes the rotated pair back atomically, so Claude Code is NOT required on this
box: either reuse a co-located Claude Code login (~/.claude/.credentials.json) or run
`python claude_usage_server.py --login` once to mint the collector's own credentials.
The token is NEVER logged, shown, or sent anywhere but Anthropic.

Config is read from environment / a .env beside this file and is editable from the terminal.
"""
import base64, hashlib, json, os, secrets, tempfile, time, threading, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
    def TZ(): return ZoneInfo(CONFIG["TZ"])
except Exception:
    def TZ(): return None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
START_TS = time.time()

EDITABLE = ["CITY", "LAT", "LON", "TZ", "USAGE_EVERY", "WEATHER_EVERY", "PORT", "DEVICE_URL",
            "CLAUDE_CREDENTIALS", "REFRESH_MARGIN_MIN",
            # --- reset notifications (non-secret; secrets live in SECRET_KEYS below) ---
            "NOTIFY_SESSION_RESET", "NOTIFY_SESSION_MAXED", "NOTIFY_WEEK_RESET", "NOTIFY_AUTH",
            "NOTIFY_EMAIL",
            "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_FROM", "SMTP_USER", "NOTIFY_EMAIL_TO"]
# Bearer secrets: settable from the (unauthenticated, LAN) terminal but NEVER read back —
# /api/state reports only "<key>_set": bool, and a save that submits the mask leaves them intact.
SECRET_KEYS = ["NOTIFY_DISCORD_WEBHOOK", "NOTIFY_SLACK_WEBHOOK", "SMTP_PASS"]
SECRET_MASK = "********"   # what the terminal shows for a set secret; submitting it = "unchanged"
DEFAULTS = {"CITY": "Melbourne", "LAT": "-37.8136", "LON": "144.9631", "TZ": "Australia/Melbourne",
            "USAGE_EVERY": "150", "WEATHER_EVERY": "900", "PORT": "8088",
            "DEVICE_URL": "http://claudetv.local",
            "CLAUDE_CREDENTIALS": "", "REFRESH_MARGIN_MIN": "30",
            "NOTIFY_SESSION_RESET": "false", "NOTIFY_SESSION_MAXED": "false",
            "NOTIFY_WEEK_RESET": "false", "NOTIFY_AUTH": "true", "NOTIFY_EMAIL": "false",
            "SMTP_HOST": "", "SMTP_PORT": "587", "SMTP_SECURITY": "starttls", "SMTP_FROM": "",
            "SMTP_USER": "", "NOTIFY_EMAIL_TO": "",
            "NOTIFY_DISCORD_WEBHOOK": "", "NOTIFY_SLACK_WEBHOOK": "", "SMTP_PASS": ""}
CONFIG = {}

WMO = {0:"Clear",1:"Clear",2:"Cloudy",3:"Overcast",45:"Fog",48:"Fog",51:"Drizzle",53:"Drizzle",
       55:"Drizzle",61:"Rain",63:"Rain",65:"Heavy rain",66:"Rain",67:"Rain",71:"Snow",73:"Snow",
       75:"Snow",77:"Snow",80:"Showers",81:"Showers",82:"Showers",85:"Snow",86:"Snow",95:"Storm",96:"Storm",99:"Storm"}

def load_config():
    env = dict(DEFAULTS)
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); k = k.strip().replace("CLAUDETV_", "")
                if k in DEFAULTS: env[k] = v.strip()
    for k in DEFAULTS:
        ev = os.environ.get("CLAUDETV_" + k)
        if ev is None and k == "CLAUDE_CREDENTIALS": ev = os.environ.get(k)
        if ev: env[k] = ev
    return env

def save_config(updates):
    for k, v in updates.items():
        if k in EDITABLE:
            CONFIG[k] = str(v).strip()
        elif k in SECRET_KEYS:                         # write-only: blank or the mask = leave as-is
            nv = str(v).strip()
            if nv and nv != SECRET_MASK: CONFIG[k] = nv
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("# ClaudeTV collector config (managed by the master terminal)\n")
        for k in EDITABLE + SECRET_KEYS:
            key = k if k == "CLAUDE_CREDENTIALS" else "CLAUDETV_" + k
            f.write("%s=%s\n" % (key, CONFIG.get(k, "")))
    try: os.chmod(ENV_PATH, 0o600)                      # .env now holds webhook URLs + SMTP pass
    except OSError: pass

CONFIG = load_config()
PORT = int(CONFIG["PORT"])
# Credential store resolution: an explicit config path wins; else the collector's own store
# (created by --login, lives beside .env so it follows the install dir); else a co-located
# Claude Code login. Both stores use Claude Code's {"claudeAiOauth": {...}} format.
CRED_OWN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
CRED_CLAUDE_CODE = os.path.expanduser("~/.claude/.credentials.json")
def cred_stores():
    """Ordered list of available credential stores. When BOTH exist they are independent OAuth
    token families for the same account (Anthropic allows concurrent logins), giving a hot
    standby: the keeper keeps every family's rolling window fresh and the poller fails over
    if the primary is rejected. An explicit config path pins a single store."""
    if CONFIG["CLAUDE_CREDENTIALS"]: return [os.path.expanduser(CONFIG["CLAUDE_CREDENTIALS"])]
    return [p for p in (CRED_OWN, CRED_CLAUDE_CODE) if os.path.exists(p)] or [CRED_CLAUDE_CODE]
def cred_path(): return cred_stores()[0]
def wx_url(): return ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
    "&current=temperature_2m,weather_code,apparent_temperature,relative_humidity_2m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto&forecast_days=1"
    ) % (CONFIG["LAT"], CONFIG["LON"])

_lock = threading.Lock()
_usage = None; _usage_ts = 0; _usage_err = "starting"; _wx = None; _wx_err = ""
_last_refresh = 0; _refresh_err = ""; _refreshing = False; _auth_dead = False
_cred_used = ""       # store that served the last successful poll (standby detection + display)
_dead_alerted = False  # one auth-dead alert per outage episode

# ---------- token keeper ----------
def _creds(path=None):
    p = path or cred_path()
    for _ in range(3):
        try: return json.load(open(p, encoding="utf-8"))["claudeAiOauth"]
        except Exception: time.sleep(0.2)
    return json.load(open(p, encoding="utf-8"))["claudeAiOauth"]

# Anthropic's public OAuth client (the one Claude Code itself uses). Not a secret: it is a
# public PKCE client id, the same value shipped in every Claude Code install.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_REDIRECT = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = "org:create_api_key user:profile user:inference"

def _oauth_post(payload):
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "ClaudeTV/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def _write_creds(resp, path):
    """Merge a token-endpoint response into the credentials file ATOMICALLY (tmp+rename), in
    Claude Code's format so a co-located CLI keeps working off the same file. Rotation matters:
    each refresh invalidates the old pair, so a lost write here = a dead login."""
    now_ms = int(time.time() * 1000)
    full = {}
    try: full = json.load(open(path, encoding="utf-8"))
    except Exception: pass
    d = full.get("claudeAiOauth") or {}
    d["accessToken"] = resp["access_token"]
    if resp.get("refresh_token"): d["refreshToken"] = resp["refresh_token"]
    if resp.get("expires_in"): d["expiresAt"] = now_ms + int(resp["expires_in"]) * 1000
    if resp.get("refresh_token_expires_in"):
        d["refreshTokenExpiresAt"] = now_ms + int(resp["refresh_token_expires_in"]) * 1000
    if resp.get("scope"): d.setdefault("scopes", resp["scope"].split())
    d.setdefault("subscriptionType", "unknown")
    full["claudeAiOauth"] = d
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(full, f)
    os.replace(tmp, path)

def refresh_token(reason="", path=None):
    """Refresh the OAuth access token NATIVELY via the token endpoint (no Claude Code needed) and
    write the rotated pair back. The refresh token's ~28-day window rolls forward on every refresh,
    so this keeps one login alive indefinitely. Returns True on success. Does NOT decide auth-dead:
    that is driven solely by whether the USAGE endpoint accepts the token."""
    global _last_refresh, _refresh_err, _refreshing
    if _refreshing: return False
    _refreshing = True
    try:
        path = path or cred_path()
        try:
            rt = _creds(path).get("refreshToken")
        except Exception as e:
            _refresh_err = "credentials unreadable: %s" % str(e)[:40]; return False
        if not rt:
            _refresh_err = "no refresh token in credentials (log in again)"; return False
        for attempt in (1, 2):
            try:
                resp = _oauth_post({"grant_type": "refresh_token", "refresh_token": rt,
                                    "client_id": OAUTH_CLIENT_ID})
            except urllib.error.HTTPError as e:
                body = ""
                try: body = e.read().decode()[:200]
                except Exception: pass
                if e.code == 400 and "invalid_grant" in body and attempt == 1:
                    # Someone else (e.g. an interactive Claude Code session) may have rotated the
                    # pair after we read it; re-read the file and retry once with the newer token.
                    try: nrt = _creds(path).get("refreshToken")
                    except Exception: nrt = None
                    if nrt and nrt != rt:
                        rt = nrt; continue
                _refresh_err = "refresh http %d%s" % (e.code, " (re-login needed)" if e.code == 400 else "")
                print("[%s] token refresh: %s" % (time.strftime("%H:%M:%S"), _refresh_err)); return False
            except Exception as e:
                _refresh_err = "refresh: %s" % str(e)[:50]; return False
            _write_creds(resp, path)
            _last_refresh = int(time.time()); _refresh_err = ""
            print("[%s] token refreshed (%s) valid +%dm, refresh window +%dd" % (
                time.strftime("%H:%M:%S"), reason or "keeper", int(resp.get("expires_in", 0)) / 60,
                int(resp.get("refresh_token_expires_in", 0)) / 86400))
            return True
        return False
    finally:
        _refreshing = False

def keeper():
    while True:
        try:
            margin = int(CONFIG["REFRESH_MARGIN_MIN"]) * 60 * 1000
            for path in cred_stores():                  # keep EVERY family's rolling window fresh
                if not os.path.exists(path): continue
                try: c = _creds(path)
                except Exception: continue
                # only refresh a token that HAS a refresh token; a long-lived token (no
                # refreshToken) needs no keeping alive, so don't hit the token endpoint pointlessly.
                if not c.get("refreshToken"): continue
                exp = int(c.get("expiresAt", 0)); rt_exp = int(c.get("refreshTokenExpiresAt", 0))
                # refresh near access-token expiry, and ALSO if the ~28-day refresh window has
                # somehow run below 21 days: it re-arms to ~28d on every refresh, so a low window
                # means refreshes have been stuck (stale expiresAt, clock skew) and must not wait.
                if (exp == 0 or (exp - time.time() * 1000) < margin) or \
                   (rt_exp and (rt_exp - time.time() * 1000) < 21 * 86400 * 1000):
                    refresh_token("proactive", path)
        except Exception as e:
            print("[%s] keeper error: %s" % (time.strftime("%H:%M:%S"), e))
        time.sleep(900 if _auth_dead else 120)

def token_status():
    try:
        c = _creds(); exp = c.get("expiresAt", 0) / 1000.0
        if exp and exp < time.time(): return "expired", c.get("subscriptionType", "?"), 0
        return "valid", c.get("subscriptionType", "?"), int(exp - time.time()) if exp else 0
    except Exception as e:
        return "missing", str(e)[:40], 0

def auth_state():
    """Compact state the device reacts to: ok | dead | pending.
    dead    = the usage endpoint rejected the token (401/403) -> log in again on the host
              (`--login`, or `claude /login` when co-located with Claude Code).
    pending = token expired/missing but the keeper may still recover it (transient)."""
    if _auth_dead: return "dead"
    return "ok" if token_status()[0] == "valid" else "pending"

# ---------- data fetchers ----------
def _clock(dt): h = dt.hour % 12 or 12; return "%d:%02d%s" % (h, dt.minute, "am" if dt.hour < 12 else "pm")
def _clock_short(dt):
    h = dt.hour % 12 or 12; ap = "am" if dt.hour < 12 else "pm"
    return "%d%s" % (h, ap) if dt.minute == 0 else "%d:%02d%s" % (h, dt.minute, ap)
def _parse(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    tz = TZ(); return dt.astimezone(tz) if tz else dt.astimezone()

def _scoped_weekly(data):
    """The model-scoped weekly limit (e.g. Fable), read GENERICALLY out of limits[].
    There is no top-level key for it — seven_day_opus/seven_day_sonnet are a different
    (null) thing. Shape: {kind:'weekly_scoped', percent, scope:{model:{display_name}}}.
    It shares the seven_day reset window (they land 1s apart), so it needs no own reset.
    Returns (percent, LABEL) or (-1, "") when the account has no scoped limit."""
    for lim in (data.get("limits") or []):
        if lim.get("kind") != "weekly_scoped": continue
        pct = lim.get("percent")
        if pct is None: continue
        name = (((lim.get("scope") or {}).get("model") or {}).get("display_name") or "").strip()
        return round(float(pct)), name.upper()[:7]
    return -1, ""

def fetch_usage():
    """Poll the usage endpoint, failing over across credential stores: if the primary family is
    rejected (401/403) and a standby store exists, try it before giving up. Whichever store
    answers becomes _cred_used; dropping to a standby fires a one-shot alert."""
    global _cred_used
    stores = cred_stores(); data = None; served = None; last = None
    for i, path in enumerate(stores):
        try:
            tok = _creds(path)["accessToken"]
        except Exception as e:
            last = e; continue
        req = urllib.request.Request(USAGE_URL, headers={"Authorization": "Bearer " + tok,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r: data = json.loads(r.read().decode())
            served = path; break
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403) and i < len(stores) - 1:
                print("[%s] store rejected (http %d), trying standby: %s" % (
                    time.strftime("%H:%M:%S"), e.code, stores[i + 1])); continue
            raise
    if data is None: raise last if last else RuntimeError("no credential store")
    if served != stores[0] and _cred_used != served:
        _auth_alert("standby", "Anthropic rejected the primary Claude login; the display is now "
                    "running on the standby login (%s). Everything keeps working, but re-login "
                    "the primary when convenient." % served)
    _cred_used = served
    fh, sw = data.get("five_hour") or {}, data.get("seven_day") or {}
    out = {"s": round(float(fh.get("utilization", 0))), "w": round(float(sw.get("utilization", 0))), "sr": "", "wr": ""}
    if fh.get("resets_at"): out["sr"] = _clock(_parse(fh["resets_at"]))
    if sw.get("resets_at"):
        d = _parse(sw["resets_at"]); out["wr"] = "%s %d %s" % (d.strftime("%b"), d.day, _clock_short(d))
    out["f"], out["fl"] = _scoped_weekly(data)
    return out, {"session": fh.get("resets_at"), "week": sw.get("resets_at")}

def geocode(q):
    url = "https://geocoding-api.open-meteo.com/v1/search?name=%s&count=6&language=en&format=json" % urllib.parse.quote(q)
    with urllib.request.urlopen(url, timeout=8) as r: j = json.loads(r.read().decode())
    out = []
    for h in j.get("results", []):
        loc = ", ".join(x for x in [h.get("name"), h.get("admin1"), h.get("country")] if x)
        out.append({"label": loc, "city": h.get("name", q), "lat": h.get("latitude"),
                    "lon": h.get("longitude"), "tz": h.get("timezone", "auto")})
    return out

def fetch_weather():
    with urllib.request.urlopen(wx_url(), timeout=8) as r: j = json.loads(r.read().decode())
    c, d = j.get("current", {}), j.get("daily", {})
    def di(key):
        v = d.get(key); return round(float(v[0])) if isinstance(v, list) and v and v[0] is not None else None
    return {"city": CONFIG["CITY"], "wc": WMO.get(int(c.get("weather_code", -1)), "--"),
            "wt": round(float(c.get("temperature_2m", 0))), "wfl": round(float(c.get("apparent_temperature", 0))),
            "whum": round(float(c.get("relative_humidity_2m", 0))), "whi": di("temperature_2m_max"),
            "wlo": di("temperature_2m_min"), "wrain": di("precipitation_probability_max")}

# ---------- reset notifier ----------
# Logs + notifies (email / Discord / Slack) when a usage window (5h session, 7d week) resets.
# resets_at is the NEXT *scheduled* reset on a FIXED schedule — a surprise Anthropic reset ('gift')
# zeroes your usage but does NOT move it. So a reset is detected from EITHER:
#   - resets_at rolling forward  (the scheduled reset arrived), OR
#   - utilisation dropping >= RESET_DROP  (a gift, or a scheduled reset whose resets_at lags),
# then classified by TIMING: at/after the scheduled reset time (prev resets_at) -> 'expected';
# before it -> 'gift'. Every reset (expected + gifts) is appended to resets.log; cold start baselines
# silently; fires once per reset; sends run off-thread and can never crash the poller.
NOTIFY_STATE_PATH = os.path.join(os.path.dirname(ENV_PATH), "notify_state.json")
RESET_LOG_PATH = os.path.join(os.path.dirname(ENV_PATH), "resets.log")
_notify_state = None
_reset_log = None                                      # in-memory tail of resets.log (last 30)
_notify_last = {"event": "", "at": 0, "results": {}}   # last dispatch, surfaced in the terminal

def _truthy(v): return str(v).strip().lower() in ("1", "true", "yes", "on")
def _now_utc(): return datetime.fromtimestamp(time.time(), tz=timezone.utc)
def _iso_dt(iso):
    try:
        dt = datetime.fromisoformat(iso); return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception: return None

# Drop guard: a gift (or a scheduled reset whose resets_at lags) shows as a utilisation fall; require
# >= RESET_DROP points so rounding jitter can't false-trigger. A scheduled reset also moves resets_at
# (usage-independent) so a light-usage scheduled reset is still caught; a light-usage gift is a
# non-event (nothing meaningful was freed).
RESET_DROP = 5
# A session "hit its cap" if it was at/above this before resetting. Near-max (not strictly 100) so a
# maxed session polled at 96-99% — or one that maxed between polls — isn't missed.
SESSION_MAXED_PCT = 95

def _load_notify_state():
    global _notify_state
    if _notify_state is None:
        try: _notify_state = json.load(open(NOTIFY_STATE_PATH, encoding="utf-8"))
        except Exception: _notify_state = {}
    return _notify_state

def _save_notify_state():
    try:
        with open(NOTIFY_STATE_PATH, "w", encoding="utf-8") as f: json.dump(_notify_state, f)
    except Exception as e: print("[notify] state save failed: %s" % e)

def _load_reset_log():
    global _reset_log
    if _reset_log is None:
        _reset_log = []
        try:
            for line in open(RESET_LOG_PATH, encoding="utf-8"):
                line = line.strip()
                if line: _reset_log.append(json.loads(line))
            _reset_log = _reset_log[-30:]
        except Exception: _reset_log = []
    return _reset_log

def _log_reset(window, cls, detail):
    """Append-only record of every reset — expected rollovers AND Anthropic 'gifts'."""
    entry = {"at": _now_utc().isoformat(timespec="seconds"), "window": window, "class": cls, "detail": detail}
    log = _load_reset_log(); log.append(entry); del log[:-30]
    try:
        with open(RESET_LOG_PATH, "a", encoding="utf-8") as f: f.write(json.dumps(entry) + "\n")
    except Exception as e: print("[notify] reset-log write failed: %s" % e)
    print("[%s] RESET %s (%s): %s" % (time.strftime("%H:%M:%S"), window, cls, detail))

def _reset_detail(kind, prev, u):
    """Human before->after string for the log, e.g. 'W 79%->2%, FABLE 100%->3%'."""
    parts = []
    for k in (("s",) if kind == "session" else ("w", "f")):
        cur = u.get(k)
        if cur is None or cur < 0: continue
        lbl = {"s": "S", "w": "W", "f": (u.get("fl") or "F")}[k]
        before = prev.get(k)
        parts.append("%s %s%%->%d%%" % (lbl, before if before is not None else "?", cur))
    return ", ".join(parts)

def _channels():
    """Configured sinks -> list of channel names."""
    ch = []
    if CONFIG.get("NOTIFY_DISCORD_WEBHOOK"): ch.append("discord")
    if CONFIG.get("NOTIFY_SLACK_WEBHOOK"): ch.append("slack")
    if _truthy(CONFIG.get("NOTIFY_EMAIL")) and CONFIG.get("SMTP_HOST") and CONFIG.get("NOTIFY_EMAIL_TO"):
        ch.append("email")
    return ch

def _post_json(url, payload):
    # Discord/Cloudflare 403s the default "Python-urllib/x.y" User-Agent, so set a real one.
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "ClaudeTV/1.0 (+https://github.com/latticelabs-au/ClaudeTV)"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r: r.read()

def _send_discord(title, body):
    _post_json(CONFIG["NOTIFY_DISCORD_WEBHOOK"],
               {"embeds": [{"title": title, "description": body, "color": 0xFF7A55}]})

def _send_slack(title, body):
    _post_json(CONFIG["NOTIFY_SLACK_WEBHOOK"], {"text": "*%s*\n%s" % (title, body)})

def _send_email(title, body):
    import smtplib, ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = CONFIG.get("SMTP_FROM") or CONFIG.get("SMTP_USER") or "claudetv@localhost"
    msg["To"] = CONFIG["NOTIFY_EMAIL_TO"]
    msg.set_content(body)
    host, port = CONFIG["SMTP_HOST"], int(CONFIG.get("SMTP_PORT") or 587)
    sec = (CONFIG.get("SMTP_SECURITY") or "starttls").lower()
    if sec == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(host, port, timeout=15)
        if sec == "starttls": s.starttls(context=ssl.create_default_context())
    try:
        if CONFIG.get("SMTP_USER"): s.login(CONFIG["SMTP_USER"], CONFIG.get("SMTP_PASS", ""))
        s.send_message(msg)
    finally:
        s.quit()

_SENDERS = {"discord": _send_discord, "slack": _send_slack, "email": _send_email}

def _dispatch(title, body, channels, event):
    """Send to each channel; returns {channel: 'ok'|error}. Never raises."""
    results = {}
    for c in channels:
        try:
            _SENDERS[c](title, body); results[c] = "ok"
            print("[%s] notify %s -> %s ok" % (time.strftime("%H:%M:%S"), event, c))
        except Exception as e:
            results[c] = str(e)[:120]
            print("[%s] notify %s -> %s FAILED: %s" % (time.strftime("%H:%M:%S"), event, c, results[c]))
    _notify_last.update({"event": event, "at": int(time.time()), "results": results})
    return results

_AUTH_TITLES = {"dead": "\U0001F534 ClaudeTV: Claude login dead (action needed)",
                "standby": "\U0001F7E0 ClaudeTV: failed over to standby login",
                "recovered": "\U0001F7E2 ClaudeTV: Claude auth recovered"}

def _auth_alert(event, body):
    """Auth outage/failover alerts through the configured notify channels. Edge-triggered by
    callers (one alert per episode). NOTIFY_AUTH toggle, on by default. Never raises."""
    try:
        if not (_truthy(CONFIG.get("NOTIFY_AUTH")) and _channels()): return
        threading.Thread(target=_dispatch, args=(_AUTH_TITLES[event], body, _channels(),
                         "auth_" + event), daemon=True).start()
    except Exception as e:
        print("[notify] auth alert error: %s" % e)

def _reset_message(kind, u, cls="expected", maxed=False):
    window = "session (5h)" if kind == "session" else "weekly (7d)"
    parts = ["S %d%%" % u.get("s", 0), "W %d%%" % u.get("w", 0)]
    if u.get("f", -1) >= 0: parts.append("%s %d%%" % (u.get("fl") or "F", u["f"]))
    now = " · ".join(parts)
    nxt_v = u.get("sr") if kind == "session" else u.get("wr")
    nxt = (" Next reset %s%s." % ("~" if kind == "session" else "", nxt_v)) if nxt_v else ""
    if maxed:                                          # session that had hit its cap
        return ("%s Maxed session reset — you're unblocked" % ("\U0001F381" if cls == "gift" else "✅"),
                "Your session hit its cap and just reset%s. Now: %s.%s"
                % (" EARLY — a gift!" if cls == "gift" else "", now, nxt))
    if cls == "gift":
        return ("\U0001F381 Anthropic gift — %s usage reset early" % window,
                "Your %s quota was reset ahead of schedule — free capacity. Now: %s.%s" % (window, now, nxt))
    return ("Claude %s usage reset" % window,
            "Your %s quota just refreshed. Now: %s.%s" % (window, now, nxt))

def _was_maxed(prev):
    s = prev.get("s")
    return s is not None and s >= SESSION_MAXED_PCT

def _should_notify(kind, prev):
    """Session has TWO independent toggles: NOTIFY_SESSION_RESET (every reset) and
    NOTIFY_SESSION_MAXED (only when the ending session had hit its cap). Week: NOTIFY_WEEK_RESET."""
    if kind == "session":
        return (_truthy(CONFIG.get("NOTIFY_SESSION_RESET"))
                or (_was_maxed(prev) and _truthy(CONFIG.get("NOTIFY_SESSION_MAXED"))))
    return _truthy(CONFIG.get("NOTIFY_WEEK_RESET"))

def notify_check(u, resets):
    """Detect + log usage-window resets (see the section header), then notify per the toggles.
    Per window: session=s / resets_at.five_hour; week=(w OR f) / resets_at.seven_day. Baselines
    silently on first sight; fires once per reset. Never breaks the poller."""
    try:
        st = _load_notify_state(); changed = False; now = _now_utc()
        for kind, keys in (("session", ("s",)), ("week", ("w", "f"))):
            ra_iso = resets.get(kind)
            prev = st.get(kind) if isinstance(st.get(kind), dict) else {}   # migrate old formats
            cur = dict(prev); prev_ra = _iso_dt(prev.get("ra"))
            if ra_iso: cur["ra"] = ra_iso
            usable = {}
            for k in keys:                              # f == -1 when the account has no scoped limit
                v = u.get(k)
                if v is not None and v >= 0: cur[k] = v; usable[k] = v
            reset = False
            if prev:                                    # not first sight
                new_ra = _iso_dt(ra_iso)
                rolled = bool(new_ra and prev_ra and (new_ra - prev_ra).total_seconds() > 60)
                dropped = any(prev.get(k) is not None and (prev[k] - v) >= RESET_DROP
                              for k, v in usable.items())
                reset = rolled or dropped
            ended = prev.get("ra")
            # dedup: a rolling resets_at can lag its reset, so the drop and the later ra-roll are the
            # SAME reset — fire once per ended window.
            if reset and ended is not None and prev.get("fired_for") == ended: reset = False
            if reset:
                cls = "expected" if (prev_ra and now >= prev_ra - timedelta(minutes=5)) else "gift"
                cur["fired_for"] = ended
            if cur != prev: st[kind] = cur; changed = True
            if reset:
                _log_reset(kind, cls, _reset_detail(kind, prev, u))
                if _should_notify(kind, prev) and _channels():
                    title, body = _reset_message(kind, u, cls, kind == "session" and _was_maxed(prev))
                    threading.Thread(target=_dispatch, args=(title, body, _channels(), kind + "_reset"),
                                     daemon=True).start()
        if changed: _save_notify_state()
    except Exception as e:
        print("[notify] check error: %s" % e)

def notify_test(channel):
    with _lock: u = dict(_usage) if _usage else {}
    if u:                                              # preview the REAL week-reset alert
        title, body = _reset_message("week", u)
        title = "[ClaudeTV test] " + title
        body = "This is a test of your ClaudeTV reset alerts — the real one looks like this.\n" + body
    else:
        title = "ClaudeTV test notification"
        body = "If you can read this, ClaudeTV reset alerts are wired up correctly."
    ready = _channels()
    want = [channel] if channel in _SENDERS else ready
    if not want: return {"error": "no channel configured"}
    results = {}
    for c in want:
        results.update(_dispatch(title, body, [c], "test") if c in ready else {c: "not configured"})
    return results

def notify_status():
    st = _load_notify_state()
    return {"session_enabled": _truthy(CONFIG.get("NOTIFY_SESSION_RESET")),
            "session_maxed_enabled": _truthy(CONFIG.get("NOTIFY_SESSION_MAXED")),
            "week_enabled": _truthy(CONFIG.get("NOTIFY_WEEK_RESET")),
            "channels": _channels(),
            "discord_set": bool(CONFIG.get("NOTIFY_DISCORD_WEBHOOK")),
            "slack_set": bool(CONFIG.get("NOTIFY_SLACK_WEBHOOK")),
            "smtp_pass_set": bool(CONFIG.get("SMTP_PASS")),
            "tracking": {k: v for k, v in st.items() if isinstance(v, dict)},
            "recent_resets": _load_reset_log()[-10:], "last_sent": _notify_last}

def poller():
    global _usage, _usage_ts, _usage_err, _wx, _wx_err, _auth_dead, _dead_alerted
    next_u = 0.0; backoff = int(CONFIG["USAGE_EVERY"]); next_w = 0.0
    while True:
        now = time.time()
        if now >= next_u:
            try:
                u, resets = fetch_usage()
                with _lock:
                    was_dead = _auth_dead
                    _usage = u; _usage_ts = int(now); _usage_err = ""; _auth_dead = False
                if was_dead and _dead_alerted:
                    _auth_alert("recovered", "The usage endpoint is accepting the Claude login "
                                "again. The display is back to live data.")
                _dead_alerted = False
                backoff = int(CONFIG["USAGE_EVERY"]); next_u = now + backoff
                notify_check(u, resets)                 # detect/log/notify resets (never raises)
            except urllib.error.HTTPError as e:
                with _lock: _usage_err = "http %d" % e.code
                if e.code in (401, 403):                 # EVERY credential store rejected = dead
                    with _lock: _auth_dead = True         # (only 401/403 means dead; 429 is rate-limit)
                    if not _dead_alerted:
                        _dead_alerted = True
                        _auth_alert("dead", "Anthropic rejected every Claude login on the host "
                                    "(http %d). The display shows LOGIN EXPIRED until you log in "
                                    "again: python3 claude_usage_server.py --login (or claude "
                                    "/login if Claude Code is on the box)." % e.code)
                    refresh_token("auth-fail")            # best-effort recovery (only if a refresh token exists)
                    next_u = now + 300
                else:
                    ra = e.headers.get("Retry-After"); wait = int(ra) if (ra and ra.isdigit()) else min(backoff * 2, 600)
                    backoff = wait; next_u = now + wait
            except Exception as e:
                next_u = now + 30
                with _lock: _usage_err = str(e)[:50]
        if now >= next_w:
            try:
                w = fetch_weather()
                with _lock: _wx = w; _wx_err = ""
                next_w = now + int(CONFIG["WEATHER_EVERY"])
            except Exception as e:
                next_w = now + 120
                with _lock: _wx_err = str(e)[:50]
        time.sleep(2)

def device_json():
    with _lock: u, ts, err, wx = _usage, _usage_ts, _usage_err, _wx
    st = {"ok": 1 if u else 0, "age": (int(time.time()) - ts) if ts else -1, "err": err,
          "auth": auth_state()}
    st.update(u or {"s": 0, "w": 0, "f": -1, "fl": "", "sr": "", "wr": ""})
    if wx: st.update(wx)
    return st

def full_state():
    tok, sub, exp_in = token_status()
    with _lock: u, ts, err, wx, wxe, lr, re_, refg = _usage, _usage_ts, _usage_err, _wx, _wx_err, _last_refresh, _refresh_err, _refreshing
    return {"service": {"uptime_s": int(time.time() - START_TS), "port": PORT},
            "token": {"status": tok, "plan": sub, "expires_in_s": exp_in, "auth": auth_state(),
                      "last_refresh_s": (int(time.time()) - lr) if lr else -1,
                      "refresh_err": re_, "refreshing": refg, "cred": _cred_used or cred_path(),
                      "stores": len(cred_stores()),
                      "standby": bool(_cred_used and _cred_used != cred_stores()[0])},
            "usage": {"ok": 1 if u else 0, "age": (int(time.time()) - ts) if ts else -1, "err": err, **(u or {})},
            "weather": (wx or {}), "weather_err": wxe, "config": {k: CONFIG[k] for k in EDITABLE},
            "notify": notify_status()}

def restart_later():
    def go(): time.sleep(0.5); os._exit(0)
    threading.Thread(target=go, daemon=True).start()

TERMINAL = """<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>ClaudeTV Terminal</title><style>
:root{--bg:#0a0d13;--panel:#141a26;--line:#222a39;--coral:#ff7a55;--cyan:#3fd2dd;--gray:#a4b0c2}
*{box-sizing:border-box}body{font-family:ui-monospace,Menlo,monospace;background:var(--bg);color:#e6e9ef;margin:0;padding:18px;max-width:660px;margin:auto}
h1{font-size:20px;margin:0 0 2px}h1 .c{color:var(--coral)}.sub{color:var(--cyan);font-size:12px;margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
.card h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--gray);margin:0 0 10px}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:6px 0;font-size:14px}
.big{font-size:26px;font-weight:700}.pill{padding:2px 9px;border-radius:99px;font-size:12px}
.ok{background:#10331d;color:#54d36e}.warn{background:#3a2a10;color:#f0ad36}.bad{background:#3a1320;color:#ff4d68}
label{font-size:12px;color:var(--gray);display:block;margin:8px 0 3px}
input{width:100%;background:#0d1119;color:#e6e9ef;border:1px solid var(--line);border-radius:8px;padding:9px;font:inherit}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
button{background:var(--coral);color:#1a0f0a;border:0;border-radius:8px;padding:10px 14px;font:inherit;font-weight:700;cursor:pointer;width:100%}
button.ghost{background:#1c2331;color:#e6e9ef;border:1px solid var(--line)}.muted{color:var(--gray);font-size:12px}
a{color:var(--cyan)}code{background:#0d1119;border:1px solid var(--line);border-radius:6px;padding:2px 6px;font-size:12px;word-break:break-all}
.foot{text-align:center;margin-top:16px}.foot a{color:var(--cyan);text-decoration:none;font-size:12px}
</style></head><body>
<h1>Claude<span class=c>TV</span> &middot; Master Terminal</h1>
<div class=sub>collector + control plane &middot; <a href="https://latticelabs.au" target=_blank>lattice labs</a></div>
<div class=card style="border-color:#39c3cd;background:#0d2025">
<div class=muted>Paste this into your ClaudeTV device's <b>Collector URL</b> field:</div>
<div style="display:flex;gap:8px;align-items:center;margin-top:8px">
<code id=ownUrl style="flex:1;font-size:15px;color:#3fd2dd">…</code>
<button style="width:auto;padding:8px 14px" onclick="navigator.clipboard.writeText(ownUrl.textContent);this.textContent='Copied'">Copy</button></div></div>

<div class=card><h2>Service</h2>
<div class=row><span>Status</span><span class=pill ok id=svc>running</span></div>
<div class=row><span>Uptime</span><span id=up>--</span></div>
<label style="margin-top:6px">ClaudeTV device URL</label><input id=DEVICE_URL>
<button style="background:#39c3cd;color:#06222a;font-weight:700;margin-top:8px" onclick="window.open(devUrl||'http://claudetv.local','_blank')">Open ClaudeTV device &#8599;</button>
<div class=grid style=margin-top:8px><button class=ghost onclick=restart()>Restart service</button><button class=ghost onclick=load()>Refresh</button></div></div>

<div class=card><h2>Claude token keeper</h2>
<div class=row><span>Token</span><span class=pill id=tok>--</span></div>
<div class=row><span>Expires in</span><span id=texp>--</span></div>
<div class=row><span>Last refresh</span><span id=tref>--</span></div>
<div class=row><span class=muted>credentials</span><span class=muted id=cred></span></div>
<div class=row><span class=muted id=terr></span><button style="width:auto" class=ghost onclick=refresh()>Refresh now</button></div></div>

<div class=card><h2>Live data</h2>
<div class=row><span>Session (5h)</span><span class=big id=sess>--</span></div>
<div class=row><span class=muted id=sessr></span><span class=muted id=uerr></span></div>
<div class=row><span>Week (7d)</span><span class=big id=week>--</span></div>
<div class=row id=fablerow style="display:none"><span id=fablelbl>Fable (7d)</span><span class=big id=fable>--</span></div>
<div class=row><span class=muted id=weekr></span><span class=muted id=age></span></div>
<div class=row><span id=wx class=muted></span></div></div>

<div class=card><h2>Weather &amp; timezone</h2>
<label>Search a city (sets location + timezone automatically)</label>
<input id=citySearch placeholder="e.g. Melbourne" autocomplete=off>
<div id=geoResults style="margin-top:6px"></div>
<div class=muted style="margin-top:8px">Current: <b id=CITY_disp>--</b> <span id=geoMeta></span></div>
<input type=hidden id=CITY><input type=hidden id=LAT><input type=hidden id=LON><input type=hidden id=TZ>
<label style="margin-top:8px">Weather refresh (s)</label><input id=WEATHER_EVERY></div>

<div class=card><h2>Claude config</h2>
<label>Credentials path (blank = auto: collector's own login, else Claude Code's)</label><input id=CLAUDE_CREDENTIALS placeholder=auto>
<div class=grid><div><label>Refresh margin (min)</label><input id=REFRESH_MARGIN_MIN></div><div><label>Usage poll (s)</label><input id=USAGE_EVERY></div></div>
<label>Port</label><input id=PORT>
<div class=muted style="margin-top:8px">No Claude Code on this box? Run <code>python claude_usage_server.py --login</code> once; the keeper then renews the token itself, forever.</div></div>

<div class=card><h2>Reset notifications</h2>
<div class=muted>Get pinged when your Claude usage window rolls over to a fresh quota (the reset Anthropic only posts on X).</div>
<div class=row style="margin-top:8px"><span>Session reset (5h) &mdash; every reset</span><input type=checkbox id=NOTIFY_SESSION_RESET></div>
<div class=row><span class=muted>&nbsp;&nbsp;&#8627; only when the session maxed out (hit its cap)</span><input type=checkbox id=NOTIFY_SESSION_MAXED></div>
<div class=row><span>Week reset (7d)</span><input type=checkbox id=NOTIFY_WEEK_RESET></div>
<div class=row><span>Auth outage / failover alerts</span><input type=checkbox id=NOTIFY_AUTH></div>
<div class=muted id=nstat></div>
<label style="margin-top:10px">Discord webhook URL</label>
<div style="display:flex;gap:8px"><input id=NOTIFY_DISCORD_WEBHOOK placeholder="https://discord.com/api/webhooks/…" style="flex:1"><button class=ghost style="width:auto" onclick="ntest('discord')">Test</button></div>
<label style="margin-top:8px">Slack webhook URL</label>
<div style="display:flex;gap:8px"><input id=NOTIFY_SLACK_WEBHOOK placeholder="https://hooks.slack.com/services/…" style="flex:1"><button class=ghost style="width:auto" onclick="ntest('slack')">Test</button></div>
<div class=row style="margin-top:12px"><span>Email alerts (SMTP)</span><input type=checkbox id=NOTIFY_EMAIL></div>
<div class=grid><div><label>SMTP host</label><input id=SMTP_HOST placeholder=smtp.gmail.com></div><div><label>Port</label><input id=SMTP_PORT></div></div>
<div class=grid><div><label>Security</label><input id=SMTP_SECURITY placeholder="starttls · ssl · none"></div><div><label>From address</label><input id=SMTP_FROM placeholder=you@example.com></div></div>
<div class=grid><div><label>SMTP user</label><input id=SMTP_USER></div><div><label>SMTP password</label><input id=SMTP_PASS type=password></div></div>
<label>Send alerts to</label>
<div style="display:flex;gap:8px"><input id=NOTIFY_EMAIL_TO placeholder=you@example.com style="flex:1"><button class=ghost style="width:auto" onclick="ntest('email')">Test</button></div>
<div class=muted style="margin-top:8px">Secrets are write-only: once saved a webhook/password shows as <code>********</code> (never sent back) — leave it to keep, paste a new value to replace. Test uses the last <b>saved</b> config.</div>
<div class=row><span class=muted id=nres></span></div>
<div class=muted id=nlog style="margin-top:8px"></div></div>

<button onclick=saveCfg()>Save config &amp; restart</button>

<div class=card style=margin-top:14px><h2>Install as a service</h2>
<div class=muted>One-time, on this host (log in first: <code>--login</code>, or a co-located Claude Code login):</div>
<p><code>sudo bash install.sh</code></p>
<div class=muted>Installs the systemd unit (auto-start + auto-restart). The token keeper then keeps Claude auth alive automatically.</div></div>

<div class=foot><a href="https://latticelabs.au" target=_blank>lattice labs &middot; ClaudeTV</a></div>
<script>
let devUrl='';
ownUrl.textContent=location.origin+'/usage';
function fmtUp(s){let h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+'h '+m+'m'}
function fmtAgo(s){if(s<0)return 'never';if(s<60)return s+'s ago';let m=Math.floor(s/60);return m<60?m+'m ago':Math.floor(m/60)+'h ago'}
function pill(el,cls,txt){el.className='pill '+cls;el.textContent=txt}
function load(){fetch('/api/state').then(r=>r.json()).then(s=>{
 up.textContent=fmtUp(s.service.uptime_s);
 const t=s.token;pill(tok,t.status=='valid'?'ok':(t.status=='expired'?'warn':'bad'),t.status+' ('+t.plan+')');
 texp.textContent=t.refreshing?'refreshing…':(t.expires_in_s>0?fmtUp(t.expires_in_s):'--');
 tref.textContent=fmtAgo(t.last_refresh_s);cred.textContent=t.cred+(t.stores>1?' (+standby)':'');
 terr.textContent=(t.standby?'⚠ running on STANDBY login · ':'')+(t.refresh_err?('⚠ '+t.refresh_err):'');
 const u=s.usage;sess.textContent=u.ok?u.s+'%':'--';sessr.textContent=u.sr?('resets '+u.sr):'idle';
 week.textContent=u.ok?u.w+'%':'--';weekr.textContent=u.wr?('resets '+u.wr):'';
 if(u.f>=0){fablerow.style.display='';fablelbl.textContent=(u.fl||'Fable')+' (7d)';fable.textContent=u.ok?u.f+'%':'--';}else fablerow.style.display='none';
 uerr.textContent=u.err?('⚠ '+u.err):'';age.textContent=u.age>=0?('updated '+u.age+'s ago'):'';
 const w=s.weather;wx.textContent=w.city?(w.city+' '+w.wt+'°C '+w.wc+' · feels '+w.wfl+'° · '+w.wlo+'/'+w.whi+'° · rain '+w.wrain+'%'):'weather --';
 for(const k in s.config){const el=document.getElementById(k);if(el&&document.activeElement!==el){
   if(el.type=='checkbox')el.checked=(s.config[k]=='true');else el.value=s.config[k];}}
 const n=s.notify||{};
 // a saved secret shows the mask as its VALUE (looks filled = obviously saved); saveCfg skips it
 [['discord_set',NOTIFY_DISCORD_WEBHOOK],['slack_set',NOTIFY_SLACK_WEBHOOK],['smtp_pass_set',SMTP_PASS]].forEach(([k,el])=>{
   if(n[k]&&document.activeElement!==el&&!el.value)el.value='********';});
 nstat.textContent='Channels: '+((n.channels||[]).join(', ')||'none configured');
 const ls=n.last_sent||{};if(ls.event)nres.textContent='last: '+ls.event+' — '+Object.entries(ls.results||{}).map(([k,v])=>k+' '+v).join(', ');
 const rr=n.recent_resets||[];nlog.innerHTML=rr.length?('<b>Recent resets</b><br>'+rr.slice().reverse().map(e=>e.at.slice(0,16).replace('T',' ')+' · '+e.window+' · '+(e.class=='gift'?'🎁 gift':'scheduled')+(e.detail?(' · '+e.detail):'')).join('<br>')):'';
 CITY_disp.textContent=s.config.CITY||'--';geoMeta.textContent=s.config.LAT?('· '+s.config.TZ):'';
 devUrl=s.config.DEVICE_URL||'';
}).catch(()=>{pill(svc,'bad','unreachable')})}
let geoT;
citySearch.oninput=function(){clearTimeout(geoT);const q=this.value.trim();if(q.length<2){geoResults.innerHTML='';return;}
 geoT=setTimeout(()=>{fetch('/api/geocode?q='+encodeURIComponent(q)).then(r=>r.json()).then(rs=>{geoResults.innerHTML='';
  rs.forEach(h=>{const b=document.createElement('button');b.className='ghost';b.style.marginBottom='4px';b.textContent=h.label;b.onclick=()=>pickCity(h);geoResults.appendChild(b);});});},350);};
function pickCity(h){geoResults.innerHTML='';citySearch.value='';pill(svc,'warn','applying…');
 fetch('/api/config?CITY='+encodeURIComponent(h.city)+'&LAT='+h.lat+'&LON='+h.lon+'&TZ='+encodeURIComponent(h.tz),{method:'POST'}).then(()=>setTimeout(load,3500));}
function saveCfg(){const ks=['CITY','LAT','LON','TZ','WEATHER_EVERY','DEVICE_URL','CLAUDE_CREDENTIALS','REFRESH_MARGIN_MIN','USAGE_EVERY','PORT',
  'SMTP_HOST','SMTP_PORT','SMTP_SECURITY','SMTP_FROM','SMTP_USER','NOTIFY_EMAIL_TO'];
 const parts=ks.map(k=>k+'='+encodeURIComponent(document.getElementById(k).value));
 ['NOTIFY_SESSION_RESET','NOTIFY_SESSION_MAXED','NOTIFY_WEEK_RESET','NOTIFY_AUTH','NOTIFY_EMAIL'].forEach(k=>parts.push(k+'='+(document.getElementById(k).checked?'true':'false')));
 ['NOTIFY_DISCORD_WEBHOOK','NOTIFY_SLACK_WEBHOOK','SMTP_PASS'].forEach(k=>{const v=document.getElementById(k).value.trim();if(v&&v!=='********')parts.push(k+'='+encodeURIComponent(v));});
 if(!confirm('Save config and restart the collector?'))return;
 fetch('/api/config?'+parts.join('&'),{method:'POST'}).then(()=>{pill(svc,'warn','restarting');setTimeout(load,3500)})}
function ntest(ch){nres.textContent='testing '+ch+'…';
 fetch('/api/notify-test?channel='+ch,{method:'POST'}).then(r=>r.json()).then(d=>{
  nres.textContent=Object.entries(d.results||{}).map(([k,v])=>k+': '+v).join(' · ')||'no channel configured';}).catch(()=>{nres.textContent='test failed'});}
function restart(){if(!confirm('Restart collector?'))return;fetch('/api/service?action=restart',{method:'POST'}).then(()=>{pill(svc,'warn','restarting');setTimeout(load,3500)})}
function refresh(){pill(tok,'warn','refreshing');fetch('/api/service?action=refresh',{method:'POST'}).then(()=>setTimeout(load,8000))}
load();setInterval(load,3000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, ctype, body):
        if isinstance(body, str): body = body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _q(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if "Content-Length" in self.headers:
            b = self.rfile.read(int(self.headers["Content-Length"])).decode()
            q.update(urllib.parse.parse_qs(b))
        return u.path, {k: v[0] for k, v in q.items()}
    def do_GET(self):
        path, _ = self._q()
        if path.startswith("/usage"): self._send(200, "application/json", json.dumps(device_json(), separators=(",", ":")))
        elif path == "/api/state": self._send(200, "application/json", json.dumps(full_state()))
        elif path == "/api/geocode":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
            try: self._send(200, "application/json", json.dumps(geocode(q)) if q else "[]")
            except Exception as e: self._send(200, "application/json", "[]")
        else: self._send(200, "text/html", TERMINAL)
    def do_POST(self):
        path, q = self._q()
        if path == "/api/config":
            save_config(q); self._send(200, "application/json", '{"ok":1}'); restart_later()
        elif path == "/api/service" and q.get("action") == "restart":
            self._send(200, "application/json", '{"ok":1}'); restart_later()
        elif path == "/api/service" and q.get("action") == "refresh":
            self._send(200, "application/json", '{"ok":1}'); threading.Thread(target=lambda: refresh_token("manual"), daemon=True).start()
        elif path == "/api/notify-test":
            self._send(200, "application/json", json.dumps({"results": notify_test(q.get("channel", "all"))}))
        else: self._send(404, "text/plain", "not found")


def oauth_login():
    """One-time interactive PKCE login: mints the collector's OWN credentials (no Claude Code
    needed on this box). Open the printed URL on ANY device, log in, paste the code back here.
    The running service picks the new store up on its next poll; no restart needed."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    url = OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "code": "true", "client_id": OAUTH_CLIENT_ID, "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT, "scope": OAUTH_SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    print("\nOpen this URL in a browser (any device), log in to Claude, and copy the code:\n\n%s\n" % url)
    code = input("Paste the code here: ").strip()
    if "#" in code: code, state = code.split("#", 1)
    try:
        resp = _oauth_post({"grant_type": "authorization_code", "code": code, "state": state,
                            "client_id": OAUTH_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT,
                            "code_verifier": verifier})
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:300]
        except Exception: pass
        print("Login failed: http %d %s" % (e.code, body)); raise SystemExit(1)
    _write_creds(resp, CRED_OWN)
    print("Logged in. Credentials saved to %s (0600)." % CRED_OWN)
    print("The collector will use this store from its next poll and keep it fresh forever.")

if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        oauth_login(); raise SystemExit(0)
    threading.Thread(target=keeper, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    print("ClaudeTV collector + terminal on http://0.0.0.0:%d  (device -> /usage, terminal -> /)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
