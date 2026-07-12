#!/usr/bin/env python
"""
ClaudeTV collector + Master Terminal.

- Serves the ESP display its data at  GET /usage
- Serves a branded management terminal at  GET /  (live status, weather/Claude config CRUD,
  token-keeper status, service control)
- Polls Anthropic /api/oauth/usage (the Claude Code /usage endpoint) for session/week %,
  and open-meteo (no key) for weather. Always serves last-good; backs off on HTTP 429.

TOKEN KEEPER: the Claude OAuth access token is short-lived (~8h). This box must have
Claude Code installed + logged in; the keeper periodically runs `claude -p "ping" --model haiku`
which makes Claude Code refresh its own token and write it back to the credentials file. The
collector reads the token fresh from the credentials file each poll, so it always picks up the
refresh. (Negligible usage cost.) The token is NEVER logged, shown, or sent anywhere but Anthropic.

Config is read from environment / a .env beside this file and is editable from the terminal.
"""
import json, os, time, threading, subprocess, shutil, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
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
            "CLAUDE_CREDENTIALS", "CLAUDE_BIN", "PING_MODEL", "REFRESH_MARGIN_MIN"]
DEFAULTS = {"CITY": "Melbourne", "LAT": "-37.8136", "LON": "144.9631", "TZ": "Australia/Melbourne",
            "USAGE_EVERY": "150", "WEATHER_EVERY": "900", "PORT": "8088",
            "DEVICE_URL": "http://claudetv.local",
            "CLAUDE_CREDENTIALS": "~/.claude/.credentials.json", "CLAUDE_BIN": "",
            "PING_MODEL": "haiku", "REFRESH_MARGIN_MIN": "30"}
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
        if ev is None and k in ("CLAUDE_CREDENTIALS", "CLAUDE_BIN"): ev = os.environ.get(k)
        if ev: env[k] = ev
    return env

def save_config(updates):
    for k, v in updates.items():
        if k in EDITABLE: CONFIG[k] = str(v).strip()
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("# ClaudeTV collector config (managed by the master terminal)\n")
        for k in EDITABLE:
            key = k if k in ("CLAUDE_CREDENTIALS", "CLAUDE_BIN") else "CLAUDETV_" + k
            f.write("%s=%s\n" % (key, CONFIG[k]))

CONFIG = load_config()
PORT = int(CONFIG["PORT"])
def cred_path(): return os.path.expanduser(CONFIG["CLAUDE_CREDENTIALS"])
def wx_url(): return ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
    "&current=temperature_2m,weather_code,apparent_temperature,relative_humidity_2m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto&forecast_days=1"
    ) % (CONFIG["LAT"], CONFIG["LON"])

_lock = threading.Lock()
_usage = None; _usage_ts = 0; _usage_err = "starting"; _wx = None; _wx_err = ""
_last_refresh = 0; _refresh_err = ""; _refreshing = False; _auth_dead = False

# substrings in `claude` output that mean the OAuth session itself is dead
# (the refresh token is invalid/revoked -> a silent re-refresh can never recover it;
#  only a manual `claude /login` on this host fixes it)
_DEAD_MARKERS = ("401", "invalid authentication", "please run /login", "unauthorized",
                 "oauth token expired", "authentication_error")

# ---------- token keeper ----------
def _creds():
    for _ in range(3):
        try: return json.load(open(cred_path(), encoding="utf-8"))["claudeAiOauth"]
        except Exception: time.sleep(0.2)
    return json.load(open(cred_path(), encoding="utf-8"))["claudeAiOauth"]

def token_expiry_ms():
    try: return int(_creds().get("expiresAt", 0))
    except Exception: return 0

def find_claude():
    if CONFIG["CLAUDE_BIN"] and os.path.exists(os.path.expanduser(CONFIG["CLAUDE_BIN"])):
        return os.path.expanduser(CONFIG["CLAUDE_BIN"])
    for c in [os.path.expanduser("~/.local/bin/claude"), shutil.which("claude"),
              os.path.expanduser("~/.claude/local/claude"), "/usr/local/bin/claude"]:
        if c and os.path.exists(c): return c
    return None

def refresh_token(reason=""):
    """Force Claude Code to refresh its token via a tiny ping. Returns True on success."""
    global _last_refresh, _refresh_err, _refreshing, _auth_dead
    if _refreshing: return False
    binp = find_claude()
    if not binp:
        _refresh_err = "claude binary not found (install Claude Code on this host)"; return False
    _refreshing = True
    try:
        before = token_expiry_ms()
        env = os.environ.copy(); env.setdefault("HOME", os.path.expanduser("~"))
        # capture output (stderr merged into stdout): the ping's error text is the ONLY
        # signal that the refresh token is dead and a manual re-login is required. Never
        # contains the token itself. Previously sent to DEVNULL -> silent outages.
        p = subprocess.run([binp, "-p", "ping", "--model", CONFIG["PING_MODEL"]],
                           cwd=os.path.expanduser("~"), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
        after = token_expiry_ms()
        if after > before or after > time.time() * 1000:
            _last_refresh = int(time.time()); _refresh_err = ""; _auth_dead = False
            print("[%s] token refreshed (%s) valid +%dm" % (time.strftime("%H:%M:%S"), reason or "keeper",
                  (after - time.time()*1000)/60000))
            return True
        out = (p.stdout or b"").decode("utf-8", "replace").strip().replace("\n", " ")
        low = out.lower()
        if p.returncode != 0 and any(m in low for m in _DEAD_MARKERS):
            _auth_dead = True
            _refresh_err = "login expired - re-auth on host: claude /login"
        elif p.returncode != 0:
            _refresh_err = ("ping failed: " + out)[:90]
        else:
            _refresh_err = "ping ran but token unchanged"
        print("[%s] token refresh: %s" % (time.strftime("%H:%M:%S"), _refresh_err)); return False
    except subprocess.TimeoutExpired:
        _refresh_err = "claude ping timed out"; return False
    except Exception as e:
        _refresh_err = str(e)[:60]; return False
    finally:
        _refreshing = False

def keeper():
    while True:
        try:
            exp = token_expiry_ms()
            margin = int(CONFIG["REFRESH_MARGIN_MIN"]) * 60 * 1000
            if exp == 0 or (exp - time.time() * 1000) < margin:
                refresh_token("proactive")
        except Exception as e:
            print("[%s] keeper error: %s" % (time.strftime("%H:%M:%S"), e))
        time.sleep(120)

def token_status():
    try:
        c = _creds(); exp = c.get("expiresAt", 0) / 1000.0
        if exp and exp < time.time(): return "expired", c.get("subscriptionType", "?"), 0
        return "valid", c.get("subscriptionType", "?"), int(exp - time.time()) if exp else 0
    except Exception as e:
        return "missing", str(e)[:40], 0

def auth_state():
    """Compact state the device reacts to: ok | dead | pending.
    dead    = refresh confirmed failing (401) -> needs a manual `claude /login` on host.
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

def fetch_usage():
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": "Bearer " + _creds()["accessToken"],
        "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r: data = json.loads(r.read().decode())
    fh, sw = data.get("five_hour") or {}, data.get("seven_day") or {}
    out = {"s": round(float(fh.get("utilization", 0))), "w": round(float(sw.get("utilization", 0))), "sr": "", "wr": ""}
    if fh.get("resets_at"): out["sr"] = _clock(_parse(fh["resets_at"]))
    if sw.get("resets_at"):
        d = _parse(sw["resets_at"]); out["wr"] = "%s %d %s" % (d.strftime("%b"), d.day, _clock_short(d))
    return out

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

def poller():
    global _usage, _usage_ts, _usage_err, _wx, _wx_err, _auth_dead
    next_u = 0.0; backoff = int(CONFIG["USAGE_EVERY"]); next_w = 0.0
    while True:
        now = time.time()
        if now >= next_u:
            try:
                u = fetch_usage()
                with _lock: _usage = u; _usage_ts = int(now); _usage_err = ""; _auth_dead = False
                backoff = int(CONFIG["USAGE_EVERY"]); next_u = now + backoff
            except urllib.error.HTTPError as e:
                with _lock: _usage_err = "http %d" % e.code
                if e.code in (401, 403):                 # auth -> try refresh; back off HARD if the
                    refresh_token("auth-fail")           # session is dead (no 10s hammer -> no 429 spin)
                    next_u = now + (300 if _auth_dead else 30)
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
    st.update(u or {"s": 0, "w": 0, "sr": "", "wr": ""})
    if wx: st.update(wx)
    return st

def full_state():
    tok, sub, exp_in = token_status()
    with _lock: u, ts, err, wx, wxe, lr, re_, refg = _usage, _usage_ts, _usage_err, _wx, _wx_err, _last_refresh, _refresh_err, _refreshing
    return {"service": {"uptime_s": int(time.time() - START_TS), "port": PORT},
            "token": {"status": tok, "plan": sub, "expires_in_s": exp_in, "auth": auth_state(),
                      "last_refresh_s": (int(time.time()) - lr) if lr else -1,
                      "refresh_err": re_, "refreshing": refg, "claude_bin": find_claude() or "NOT FOUND"},
            "usage": {"ok": 1 if u else 0, "age": (int(time.time()) - ts) if ts else -1, "err": err, **(u or {})},
            "weather": (wx or {}), "weather_err": wxe, "config": {k: CONFIG[k] for k in EDITABLE}}

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
<div class=row><span class=muted>claude bin</span><span class=muted id=cbin></span></div>
<div class=row><span class=muted id=terr></span><button style="width:auto" class=ghost onclick=refresh()>Refresh now</button></div></div>

<div class=card><h2>Live data</h2>
<div class=row><span>Session (5h)</span><span class=big id=sess>--</span></div>
<div class=row><span class=muted id=sessr></span><span class=muted id=uerr></span></div>
<div class=row><span>Week (7d)</span><span class=big id=week>--</span></div>
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
<label>Credentials path (token read from here; never stored/shown)</label><input id=CLAUDE_CREDENTIALS>
<label>Claude binary (blank = auto-detect)</label><input id=CLAUDE_BIN>
<div class=grid3><div><label>Ping model</label><input id=PING_MODEL></div><div><label>Refresh margin (min)</label><input id=REFRESH_MARGIN_MIN></div><div><label>Usage poll (s)</label><input id=USAGE_EVERY></div></div>
<label>Port</label><input id=PORT></div>

<button onclick=saveCfg()>Save config &amp; restart</button>

<div class=card style=margin-top:14px><h2>Install as a service</h2>
<div class=muted>One-time, on this host (needs Claude Code installed + logged in):</div>
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
 tref.textContent=fmtAgo(t.last_refresh_s);cbin.textContent=t.claude_bin;terr.textContent=t.refresh_err?('⚠ '+t.refresh_err):'';
 const u=s.usage;sess.textContent=u.ok?u.s+'%':'--';sessr.textContent=u.sr?('resets '+u.sr):'idle';
 week.textContent=u.ok?u.w+'%':'--';weekr.textContent=u.wr?('resets '+u.wr):'';
 uerr.textContent=u.err?('⚠ '+u.err):'';age.textContent=u.age>=0?('updated '+u.age+'s ago'):'';
 const w=s.weather;wx.textContent=w.city?(w.city+' '+w.wt+'°C '+w.wc+' · feels '+w.wfl+'° · '+w.wlo+'/'+w.whi+'° · rain '+w.wrain+'%'):'weather --';
 for(const k in s.config){const el=document.getElementById(k);if(el&&document.activeElement!==el)el.value=s.config[k];}
 CITY_disp.textContent=s.config.CITY||'--';geoMeta.textContent=s.config.LAT?('· '+s.config.TZ):'';
 devUrl=s.config.DEVICE_URL||'';
}).catch(()=>{pill(svc,'bad','unreachable')})}
let geoT;
citySearch.oninput=function(){clearTimeout(geoT);const q=this.value.trim();if(q.length<2){geoResults.innerHTML='';return;}
 geoT=setTimeout(()=>{fetch('/api/geocode?q='+encodeURIComponent(q)).then(r=>r.json()).then(rs=>{geoResults.innerHTML='';
  rs.forEach(h=>{const b=document.createElement('button');b.className='ghost';b.style.marginBottom='4px';b.textContent=h.label;b.onclick=()=>pickCity(h);geoResults.appendChild(b);});});},350);};
function pickCity(h){geoResults.innerHTML='';citySearch.value='';pill(svc,'warn','applying…');
 fetch('/api/config?CITY='+encodeURIComponent(h.city)+'&LAT='+h.lat+'&LON='+h.lon+'&TZ='+encodeURIComponent(h.tz),{method:'POST'}).then(()=>setTimeout(load,3500));}
function saveCfg(){const ks=['CITY','LAT','LON','TZ','WEATHER_EVERY','DEVICE_URL','CLAUDE_CREDENTIALS','CLAUDE_BIN','PING_MODEL','REFRESH_MARGIN_MIN','USAGE_EVERY','PORT'];
 const q=ks.map(k=>k+'='+encodeURIComponent(document.getElementById(k).value)).join('&');
 if(!confirm('Save config and restart the collector?'))return;
 fetch('/api/config?'+q,{method:'POST'}).then(()=>{pill(svc,'warn','restarting');setTimeout(load,3500)})}
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
        else: self._send(404, "text/plain", "not found")


if __name__ == "__main__":
    threading.Thread(target=keeper, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    print("ClaudeTV collector + terminal on http://0.0.0.0:%d  (device -> /usage, terminal -> /)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
