#!/usr/bin/env python
"""
ClaudeTV collector + Master Terminal.

- Serves the ESP display its data at  GET /usage
- Serves a branded management terminal at  GET /  (accounts, quota, config, service control)
- Reads every Claude account from claude-swap (cswap) and open-meteo (no key) for weather.
  Always serves last-good; never fabricates a number it was not given.

ACCOUNTS: claude-swap is the SINGLE source, for one account or twelve. It owns credential
storage, token upkeep and per-account usage polling, so this collector holds no Claude
token and runs no OAuth at runtime — there is exactly one code path and nothing that could
rotate a token family cswap also owns. `--login` remains only as a one-time ENROLLMENT
helper for a headless box with no Claude Code: it mints a credential that `cswap add`
then adopts, and plays no part in steady-state operation.

QUOTA: with auto-switching, one account hitting its cap is a non-event — cswap moves to
another. The state worth alerting on is every account being out at once, judged against
cswap's own autoswitch.threshold so the two never disagree.

Config is read from environment / a .env beside this file and is editable from the terminal.
"""
import base64, hashlib, json, os, re, secrets, shutil, signal, subprocess, tempfile, time, threading, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
    def TZ(): return ZoneInfo(CONFIG["TZ"])
except Exception:
    def TZ(): return None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
START_TS = time.time()

EDITABLE = ["CITY", "LAT", "LON", "TZ", "USAGE_EVERY", "WEATHER_EVERY", "PORT", "DEVICE_URL",
            # --- accounts: claude-swap is the single source, for one account or many ---
            "CSWAP_BIN", "CSWAP_ACCOUNTS", "UPDATE_EVERY",
            # --- accounts: codex is the optional second source, one CODEX_HOME per account ---
            "CODEX_BIN", "CODEX_ACCOUNTS", "CODEX_EVERY", "CODEX_TIMEOUT", "CODEX_SCOPED",
            "CODEX_MAXED_THRESHOLD",
            "NOTIFY_FLEET_MAXED", "MAXED_THRESHOLD",
            # --- reset notifications (non-secret; secrets live in SECRET_KEYS below) ---
            "NOTIFY_SESSION_RESET", "NOTIFY_SESSION_MAXED", "NOTIFY_WEEK_RESET", "NOTIFY_AUTH",
            "NOTIFY_EMAIL",
            "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_FROM", "SMTP_USER", "NOTIFY_EMAIL_TO"]
# Bearer secrets: settable from the (unauthenticated, LAN) terminal but NEVER read back —
# /api/state reports only "<key>_set": bool, and a save that submits the mask leaves them intact.
SECRET_KEYS = ["NOTIFY_DISCORD_WEBHOOK", "NOTIFY_SLACK_WEBHOOK", "SMTP_PASS"]
SECRET_MASK = "********"   # what the terminal shows for a set secret; submitting it = "unchanged"
DEFAULTS = {"CITY": "Melbourne", "LAT": "-37.8136", "LON": "144.9631", "TZ": "Australia/Melbourne",
            # Each `cswap list --json` call refreshes cswap's stalest account, so with N accounts
            # a given account lands every ~N polls: 90s keeps two accounts under ~3 min stale.
            "USAGE_EVERY": "90", "WEATHER_EVERY": "900", "PORT": "8088",
            "DEVICE_URL": "http://claudetv.local",
            "CSWAP_BIN": "", "CSWAP_ACCOUNTS": "", "UPDATE_EVERY": "21600",
            # Every Codex read is one live backend request per account, against a private
            # endpoint, so this is deliberately slow. 300 is also the enforced floor.
            "CODEX_BIN": "", "CODEX_ACCOUNTS": "", "CODEX_EVERY": "300", "CODEX_TIMEOUT": "20",
            "CODEX_SCOPED": "", "CODEX_MAXED_THRESHOLD": "100",
            # blank threshold = follow cswap's own autoswitch.threshold
            "NOTIFY_FLEET_MAXED": "true", "MAXED_THRESHOLD": "",
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
            key = "CLAUDETV_" + k
            f.write("%s=%s\n" % (key, CONFIG.get(k, "")))
    try: os.chmod(ENV_PATH, 0o600)                      # .env now holds webhook URLs + SMTP pass
    except OSError: pass

CONFIG = load_config()
PORT = int(CONFIG["PORT"])
# Where `--login` drops a freshly minted credential for `cswap add` to adopt: Claude Code's
# own {"claudeAiOauth": {...}} format in its own location, because that is what cswap reads.
CRED_OWN = os.path.expanduser("~/.claude/.credentials.json")
def wx_url(): return ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
    "&current=temperature_2m,weather_code,apparent_temperature,relative_humidity_2m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto&forecast_days=1"
    ) % (CONFIG["LAT"], CONFIG["LON"])

_lock = threading.Lock()
_accounts = []        # ordered account records; see cswap_accounts_from_json for the shape
_usage_ts = 0; _usage_err = "starting"; _wx = None; _wx_err = ""
_source_err = ""      # why cswap could not be read, surfaced in the dashboards
_alerted = {}         # per-account: one auth-dead alert per outage episode
_migrated = False     # legacy single-account notify state re-homed onto the primary account
_fleet = {}           # last fleet verdict, surfaced in the terminal and the device payload
_force_poll = False   # dashboards can demand an immediate re-read instead of waiting for the timer

# ---------- one-time login helper (enrollment only; cswap owns all upkeep) ----------
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

# ---------- data fetchers ----------
def _clock(dt): h = dt.hour % 12 or 12; return "%d:%02d%s" % (h, dt.minute, "am" if dt.hour < 12 else "pm")
def _clock_short(dt):
    h = dt.hour % 12 or 12; ap = "am" if dt.hour < 12 else "pm"
    return "%d%s" % (h, ap) if dt.minute == 0 else "%d:%02d%s" % (h, dt.minute, ap)
def _parse(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    tz = TZ(); return dt.astimezone(tz) if tz else dt.astimezone()

# ---------- accounts: cswap backend (multi-account) + native single-account fallback ----------
# cswap (github.com/realiti4/claude-swap) already owns multi-account credential storage, token
# keeping and per-account usage polling, and publishes all of it as `cswap list --json`. When it
# is installed we read accounts from it and this collector does no OAuth of its own; when it is
# absent everything above (keeper, refresh, store failover, --login) still runs and serves a
# single account, so an existing install keeps working untouched after an upgrade.
CSWAP_SCHEMA = 1
LABEL_MAX = 8                      # "PERSONAL" — the widest label the device header fits

# cswap's usageStatus vocabulary (claude_swap/json_output.py: usage_fields). Two things matter:
# `usage` is None for EVERY status except ok (display numbers move to lastGoodUsage), and only
# some statuses actually need a human:
#   ok                   usage present
#   token_expired        transient — cswap defers the refresh and retries automatically
#   api_key              managed API-key account: no subscription quota exists at all
#   keychain_unavailable transient — the active keychain is unreadable
#   foreign_credential   transient — the live credential belongs to another account mid-switch,
#                        "a switch repairs the drift". This is the ordinary `cswap auto` window.
#   unavailable/unknown/error   transient — the usage fetch failed
#   relogin_required / no_credentials / expired   genuinely needs a human to log in again
CSWAP_DEAD = {"relogin_required", "no_credentials", "expired"}

def cswap_bin():
    """Configured path, else a venv beside this script, else the first `cswap` on PATH."""
    explicit = (CONFIG.get("CSWAP_BIN") or "").strip()
    if explicit: return explicit if os.path.exists(explicit) else ""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "venv", "bin", "cswap"),
                 os.path.join(here, "venv", "Scripts", "cswap.exe")):
        if os.path.exists(cand): return cand
    return shutil.which("cswap") or ""

_cswap_ver = {"bin": "", "ver": ""}
def cswap_version():
    """`cswap --version`, cached per binary path so the dashboards can show it for free."""
    exe = cswap_bin()
    if not exe: return ""
    if _cswap_ver["bin"] != exe:
        v = ""
        try:
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
            v = (p.stdout or p.stderr or "").strip().splitlines()[0][:40]
        except Exception: pass
        _cswap_ver.update({"bin": exe, "ver": v})
    return _cswap_ver["ver"]

def _label(alias, email, number):
    raw = (alias or (email or "").split("@")[0] or "acct%s" % number)
    return raw.strip().upper()[:LABEL_MAX]

def _pct(d):
    v = (d or {}).get("pct")
    return round(float(v)) if v is not None else None

def cswap_accounts_from_json(doc, only=""):
    """Map a `cswap list --json` document into ordered account records.

    Record: {key, label, email, active, u:{s,w,sr,wr,f,fl}, resets:{session,week}, auth, age, err}
    `u` deliberately keeps the SHAPE the single-account build produced, so the notifier, the
    reset formatter and the device payload all keep working on it unchanged. Reset strings are
    re-rendered from resetsAt with THIS project's formatters (not cswap's `clock` field) so the
    device card geometry, which is sized off those exact strings, still fits.
    `only` is a comma list of aliases/emails/slots: it both filters and orders the result."""
    ver = doc.get("schemaVersion")
    if ver != CSWAP_SCHEMA:
        raise ValueError("unsupported cswap schemaVersion %r (expected %d)" % (ver, CSWAP_SCHEMA))
    recs, match = [], []
    for a in doc.get("accounts") or []:
        status = (a.get("usageStatus") or "ok").strip().lower()
        # usage is None for EVERY non-ok status; display-grade numbers move to lastGoodUsage.
        usage = a.get("usage")
        stale = not isinstance(usage, dict)
        if stale: usage = a.get("lastGoodUsage") or {}
        fh, sd = usage.get("fiveHour"), usage.get("sevenDay")
        # usage_to_json emits fiveHour/sevenDay/scoped ONLY when present, so an ok row can still
        # be partial. Treat that as stale too: coercing an absent window to 0% is exactly what
        # made a routine `cswap` account switch look like a 100%-to-0% Anthropic "gift" reset.
        if not isinstance(fh, dict) or not isinstance(sd, dict): stale = True
        fh, sd = fh or {}, sd or {}
        scoped = next((x for x in (usage.get("scoped") or []) if _pct(x) is not None), None)
        s_pct, w_pct = _pct(fh), _pct(sd)
        # -1 means "not known", never 0. Nothing downstream may invent a number it was not given.
        u = {"s": s_pct if s_pct is not None else -1,
             "w": w_pct if w_pct is not None else -1, "sr": "", "wr": "",
             "f": _pct(scoped) if scoped else -1,
             "fl": (scoped.get("name") or "").strip().upper()[:7] if scoped else ""}
        if fh.get("resetsAt"): u["sr"] = _clock(_parse(fh["resetsAt"]))
        if sd.get("resetsAt"):
            d = _parse(sd["resetsAt"]); u["wr"] = "%s %d %s" % (d.strftime("%b"), d.day, _clock_short(d))
        email, num = a.get("email") or "", a.get("number", "?")
        recs.append({"key": "%s:%s" % (num, email), "label": _label(a.get("alias"), email, num),
                     "email": email, "active": bool(a.get("active")), "u": u, "stale": stale,
                     "disabled": bool(a.get("disabled")),
                     "resets": {"session": fh.get("resetsAt"), "week": sd.get("resetsAt")},
                     # Only a status that genuinely needs a human is "dead". Transient ones
                     # (token_expired, foreign_credential mid-switch, unavailable) must NOT
                     # flip the device to LOGIN EXPIRED or fire an auth alert.
                     "auth": "dead" if status in CSWAP_DEAD else "ok",
                     "age": int(a.get("usageAgeSeconds") or a.get("lastGoodAgeSeconds") or 0),
                     "err": "" if status == "ok" else status})
        match.append({str(num), (a.get("alias") or "").lower(), email.lower(),
                      recs[-1]["label"].lower()} - {""})
    want = [w.strip().lower() for w in (only or "").split(",") if w.strip()]
    if not want: return recs
    picked = []
    for w in want:
        for rec, keys in zip(recs, match):
            if w in keys and rec not in picked:
                picked.append(rec); break
    return picked

# ---------- fleet exhaustion (the "you are actually blocked" signal) ----------
# With cswap auto-switching, ONE account hitting its cap is a non-event: cswap moves to another
# account and Claude keeps working. The alert that matters is when NO account has headroom left.
# cswap's own switch policy decides what "no headroom" means, so read it from cswap rather than
# inventing a second threshold that could disagree with the thing doing the switching:
#   trigger    binding window = MAX(5h, 7d [, model]) >= threshold  -> switch away
#   guard      candidate's binding window < threshold
#   hysteresis candidate must beat the current one by >= hysteresisPct
#   tiebreak   consume-first prefers the soonest-resetting weekly that still has room
#   cooldown   floor between proactive switches, bypassed at a hard limit
# Only the threshold decides exhaustion; hysteresis/cooldown/strategy are surfaced for context.
SWITCH_DEFAULTS = {"threshold": 90.0, "hysteresis": 10.0, "cooldown": 300.0,
                   "strategy": "best", "model": None}
_SWITCH_KEYS = {"autoswitch.threshold": "threshold", "autoswitch.hysteresisPct": "hysteresis",
                "autoswitch.cooldownSeconds": "cooldown", "autoswitch.strategy": "strategy",
                "autoswitch.model": "model"}

def switch_policy_from_json(doc):
    """Resolve the effective policy: cswap's settings, then any ClaudeTV override on top."""
    p = dict(SWITCH_DEFAULTS)
    for s in ((doc or {}).get("settings") or []):
        key = _SWITCH_KEYS.get(s.get("key"))
        if not key: continue
        v = s.get("value")
        if v is None and key != "model": continue
        p[key] = v if key in ("strategy", "model") else float(v)
    override = (CONFIG.get("MAXED_THRESHOLD") or "").strip()
    if override:
        try: p["threshold"] = float(override)
        except ValueError: print("[fleet] ignoring non-numeric MAXED_THRESHOLD %r" % override)
    return p

_policy_cache = {"at": 0, "p": None}
def switch_policy():
    """Cached `cswap config --json`; re-read every 10 min so tuning cswap takes effect."""
    if _policy_cache["p"] and time.time() - _policy_cache["at"] < 600:
        # the override is cheap and may change from the terminal, so re-apply it every call
        return switch_policy_from_json(_policy_cache.get("raw"))
    raw = None
    exe = cswap_bin()
    if exe:
        try:
            r = subprocess.run([exe, "config", "--json"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0: raw = json.loads(r.stdout)
        except Exception as e:
            print("[fleet] cswap config unreadable (%s); using defaults" % str(e)[:60])
    _policy_cache.update({"at": time.time(), "raw": raw})
    p = switch_policy_from_json(raw)
    _policy_cache["p"] = p
    return p

def binding_pct(rec, policy):
    """The window that will trigger a switch first: the worst of 5h and 7d, plus the model-scoped
    weekly only when cswap is configured to fold that model into the decision."""
    u = rec["u"]
    windows = [u.get("s", -1), u.get("w", -1)]
    if policy.get("model") and u.get("f", -1) >= 0: windows.append(u["f"])
    usable = [v for v in windows if v is not None and v >= 0]
    return max(usable) if usable else -1

def _eligible(rec):
    """Accounts cswap could actually switch onto. A disabled or dead account's headroom is not
    available to you, so counting it would under-report a real block."""
    return not _bench_reason(rec)

def _bench_reason(rec):
    """Why an account is held out of rotation, worded as the remedy is. '' when it is in."""
    if rec.get("disabled"): return "disabled"
    if rec.get("auth") == "dead": return "login expired"
    return ""

def fleet_state(accounts, policy):
    """Is the whole fleet out of quota? Returns the accounts that still have room, the binding
    percentage of each, and the best remaining account.

    An account whose usage we cannot currently see blocks the "exhausted" verdict entirely: it
    might be the one with room, and a false "you are blocked" is worse than a late one."""
    rows, unknown, benched = [], 0, []
    for rec in accounts:
        b = -1 if rec.get("stale") else binding_pct(rec, policy)
        why = _bench_reason(rec)
        if why:
            # A benched account still cannot end a block, because cswap will not switch onto it.
            # But when one is sitting on real headroom it IS the reason you are stuck, and naming
            # it turns "every account is out of quota" - which reads as a broken detector when you
            # know the spare is at 7% - into something you can act on.
            if 0 <= b < policy["threshold"]:
                benched.append({"label": rec["label"], "pct": b, "why": why})
            continue
        if b < 0: unknown += 1
        else: rows.append((rec["label"], b))
    headroom = [lbl for lbl, b in rows if b < policy["threshold"]]
    best = min(rows, key=lambda r: r[1])[0] if rows else ""
    return {"exhausted": bool(rows) and not headroom and not unknown, "headroom": headroom,
            "binding": dict(rows), "best": best, "usable": len(rows), "unknown": unknown,
            "benched": benched, "threshold": policy["threshold"]}

_fleet_last = {}          # edge-trigger memory: {"exhausted": bool}
_FLEET_TITLES = {"exhausted": "\U0001F6D1 ClaudeTV: every Claude account is out of quota",
                 "benched": "\U0001F6D1 ClaudeTV: no Claude account left in rotation",
                 "recovered": "\U0001F7E2 ClaudeTV: quota available again"}

def _fleet_alert(event, body):
    """Fleet block/recovery alert. Reuses the notify channels; never raises."""
    try:
        if not (_truthy(CONFIG.get("NOTIFY_FLEET_MAXED")) and _channels()): return
        threading.Thread(target=_dispatch, args=(_FLEET_TITLES[event], body, _channels(),
                         "fleet_" + event), daemon=True).start()
    except Exception as e:
        print("[fleet] alert error: %s" % e)

def fleet_check(accounts, policy):
    """Edge-triggered: alert when the fleet runs out, and once more when room returns. A poll
    that cannot see the fleet (all stale) leaves the previous verdict standing rather than
    inventing a recovery."""
    try:
        st = fleet_state(accounts, policy)
        if not st["usable"]: return st                  # nothing to judge; hold the last verdict
        was = _fleet_last.get("exhausted")
        if st["exhausted"] and not was:
            _fleet_last["exhausted"] = True
            worst = ", ".join("%s %d%%" % (l, b) for l, b in sorted(st["binding"].items()))
            if st["benched"]:
                spare = ", ".join("%s %d%% (%s)" % (b["label"], b["pct"], b["why"])
                                  for b in st["benched"])
                _fleet_alert("benched", "Every account cswap can switch to is at or above %g%% on "
                             "its binding window, so Claude Code is blocked. In rotation: %s. Held "
                             "OUT of rotation and still has room: %s - `cswap enable <account>` "
                             "puts it back and unblocks you now."
                             % (st["threshold"], worst, spare))
            else:
                _fleet_alert("exhausted", "Every Claude account is at or above %g%% on its binding "
                             "window, so there is nothing for cswap to switch to and Claude Code is "
                             "blocked until one resets. Now: %s." % (st["threshold"], worst))
            print("[%s] FLEET blocked (%s)%s" % (time.strftime("%H:%M:%S"), worst,
                  "".join(" [benched: %s %d%% %s]" % (b["label"], b["pct"], b["why"])
                          for b in st["benched"])))
        elif was and st["headroom"]:
            # Recovery needs POSITIVE evidence that an account has room. "not exhausted" is not
            # enough: a poll where one account is unreadable also fails the exhausted test, and
            # treating that as recovery would sound the all-clear on a block still in force.
            _fleet_last["exhausted"] = False
            _fleet_alert("recovered", "%s has room again (below %g%%), so cswap can switch back "
                         "and Claude Code is usable." % (st["best"], st["threshold"]))
            print("[%s] FLEET recovered (%s)" % (time.strftime("%H:%M:%S"), st["best"]))
        elif was is None:
            _fleet_last["exhausted"] = st["exhausted"]  # baseline silently on first sight
        return st
    except Exception as e:
        print("[fleet] check error: %s" % e)
        return {"exhausted": False, "headroom": [], "binding": {}, "best": "", "usable": 0,
                "benched": [], "threshold": policy.get("threshold", 0)}

def notifiable(rec):
    """May this reading drive reset detection?

    Only a COMPLETE, FRESH poll may. A stale or partial one fed to the notifier reads as a
    plunge to 0% and fires a phantom 'gift' reset — which is how an ordinary `cswap` account
    switch ended up alerting as an Anthropic gift. Display keeps showing last-good either way;
    only the notifier is gated, because it is the part that cannot take back a false positive."""
    return not rec.get("stale") and rec["u"]["s"] >= 0 and rec["u"]["w"] >= 0

def notify_key(rec):
    """Namespace for a account's reset state: the STABLE identity, never the display label.
    cswap omits `alias` entirely when unset, so a label can flip between the alias and an
    email-derived fallback. Keying on it splits one account into two divergent histories, and
    the stale one then reads as an enormous drop the moment it is picked up again."""
    return rec.get("key") or rec["label"].lower()

def cswap_accounts():
    """Read accounts from cswap. Each call also nudges cswap to refresh its stalest account, so
    polling this on the usage timer is what keeps every account's numbers current."""
    exe = cswap_bin()
    if not exe: raise RuntimeError("cswap not found")
    p = subprocess.run([exe, "list", "--json"], capture_output=True, text=True, timeout=45)
    if p.returncode != 0:
        raise RuntimeError("cswap list exited %d: %s" % (p.returncode, (p.stderr or "").strip()[:120]))
    return cswap_accounts_from_json(json.loads(p.stdout), CONFIG.get("CSWAP_ACCOUNTS", ""))

def usage_wire(accounts, wx, primary=""):
    """Build the device payload.

    The flat keys mirror ONE account (the primary) byte-for-byte as the single-account build
    emitted them, so a v4.7 device keeps working across this upgrade with no reflash; `n` and
    `acc[]` carry the rest for multi-account firmware. `primary` (the device's ?acct=) also
    moves that account to the head of acc[], so a second device can pin a different account."""
    accts = list(accounts)
    if primary:
        p = primary.strip().lower()
        for i, rec in enumerate(accts):
            if p in (rec["label"].lower(), (rec["email"] or "").lower()):
                accts.insert(0, accts.pop(i)); break
    lead = accts[0] if accts else None
    if not accts:                                        auth = "pending"
    elif all(a["auth"] == "dead" for a in accts):        auth = "dead"
    elif len(accts) == 1:                                auth = accts[0]["auth"]
    else:                                                auth = "ok"
    st = {"ok": 1 if lead else 0, "age": lead["age"] if lead else -1,
          "err": lead["err"] if lead else "no accounts", "auth": auth, "n": len(accts)}
    st.update(lead["u"] if lead else {"s": 0, "w": 0, "f": -1, "fl": "", "sr": "", "wr": ""})
    st["acc"] = [{"l": a["label"], "auth": a["auth"], **a["u"]} for a in accts]
    if wx: st.update(wx)
    return st

# ---------- accounts: codex source (OpenAI Codex on a ChatGPT plan) ----------
# The second provider. Same contract as cswap: the collector holds no token and runs no OAuth.
# Here the middleware is Codex itself: `codex app-server` speaks JSON-RPC over stdio, and
# `account/rateLimits/read` returns the live limits without a model call. Codex's own auth
# manager refreshes the credential on that read, so polling is the keep-alive and exactly one
# component ever rotates a given login. One CODEX_HOME directory per account.
#
# Measured on the production host (codex-cli 0.154.0): a cold read is ~1.1s and ~0.6 CPU-s, so
# a fresh process per read beats a resident 93MB server. Two facts are load-bearing:
#   * `--disable plugins` cuts a launch from 7 backend requests to exactly 1
#   * Codex NEVER times out a hung backend, so the deadline and the kill are ours

CODEX_MAIN_BUCKET = "codex"
DAY_MINS, WEEK_MINS = 1440, 10080
CODEX_DEAD_HOLD = 1800                  # a dead login is re-read this often, not every poll
CODEX_DEFAULT_HOME = os.path.expanduser("~/.codex")
CODEX_HOMES_ROOT = os.path.expanduser("~/.claudetv/codex")

def _cfg_num(key, default):
    try: return float(CONFIG.get(key) or default)
    except (TypeError, ValueError): return float(default)

def codex_bin():
    """Configured path, else ~/.local/bin/codex (a systemd unit has a bare PATH), else PATH."""
    explicit = (CONFIG.get("CODEX_BIN") or "").strip()
    if explicit: return explicit if os.path.exists(explicit) else ""
    cand = os.path.expanduser("~/.local/bin/codex")
    if os.path.exists(cand): return cand
    return shutil.which("codex") or ""

_codex_ver = {"bin": "", "ver": ""}
def codex_version():
    """`codex --version`, cached per binary path so the dashboards can show it for free."""
    exe = codex_bin()
    if not exe: return ""
    if _codex_ver["bin"] != exe:
        v = ""
        try:
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
            v = (p.stdout or p.stderr or "").strip().splitlines()[0][:40]
        except Exception: pass
        _codex_ver.update({"bin": exe, "ver": v})
    return _codex_ver["ver"]

def codex_homes(only="", default_home=None, root=None, seen=()):
    """Ordered [(slot, alias, path)]. `slot` is the STABLE identity ('default' or the directory
    name) and keys everything downstream; it never depends on a reply, so it is the same on a
    good poll and a failed one. ~/.codex counts once it holds a login (or has been seen good
    this run, so a later logout shows LOGIN EXPIRED instead of vanishing); every directory
    under the root counts, logged in or not. auth.json is only ever stat'ed, never opened.
    `only` is a comma list of slots: it filters BEFORE polling and sets the order."""
    default_home = default_home or CODEX_DEFAULT_HOME; root = root or CODEX_HOMES_ROOT
    found = []
    if os.path.isfile(os.path.join(default_home, "auth.json")) or "default" in seen:
        found.append(("default", "", default_home))
    try: names = sorted(os.listdir(root))
    except OSError: names = []
    for n in names:
        if n.lower() != "default" and os.path.isdir(os.path.join(root, n)):
            found.append((n.lower(), n, os.path.join(root, n)))
    want = [w.strip().lower() for w in (only or "").split(",") if w.strip()]
    if not want: return found
    by = {h[0]: h for h in found}; picked = []
    for w in want:
        if w in by and by[w] not in picked: picked.append(by[w])
    return picked

# Codex gives no structured HTTP status, only this message shape (codex-rs backend-client):
#   "failed to fetch codex rate limits: GET <url> failed: 401 Unauthorized; content-type=...; body=..."
_CODEX_HTTP = re.compile(r"failed: (\d{3}) ")

def codex_status(account_reply, limits_reply, driver_err=""):
    """-> (auth, err). The project's original rule (56806bc): a login is dead when the USAGE
    endpoint rejects it, never because a refresh failed, and 429 is never dead. When a refresh
    fails for good, Codex keeps the stale credential and account/read still reports the account,
    so a login that needs a human arrives as HTTP 401 inside -32603, not as -32600."""
    res = (account_reply or {}).get("result")
    acct = res.get("account") if isinstance(res, dict) else None
    if isinstance(res, dict) and acct is None: return "dead", "login_required"
    if acct and acct.get("type") != "chatgpt": return "ok", "api_key"   # no subscription quota
    if driver_err: return "ok", driver_err
    e = (limits_reply or {}).get("error")
    if not e:
        return ("ok", "") if isinstance((limits_reply or {}).get("result"), dict) else ("ok", "unavailable")
    code, msg = e.get("code"), str(e.get("message") or "")
    if code == -32600: return "dead", "login_required"
    if code == -32603:
        m = _CODEX_HTTP.search(msg); http = int(m.group(1)) if m else 0
        if http == 401: return "dead", "login_expired"
        if http == 403:          # an HTML 403 is a Cloudflare challenge, not an auth verdict
            return ("ok", "blocked_by_edge") if "text/html" in msg.lower() else ("dead", "login_expired")
        if http == 429: return "ok", "rate_limited"
        return "ok", "unavailable"
    return "ok", "unknown"

def _cpct(w):
    v = (w or {}).get("usedPercent")
    return round(float(v)) if v is not None else -1

def _codex_iso(unix_s):
    """Codex reports resetsAt in unix seconds; the notifier compares ISO strings."""
    try: return datetime.fromtimestamp(int(unix_s), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError): return None

def codex_windows(bucket):
    """(short, long) windows of one bucket, classified by DURATION and never by slot: `primary`
    and `secondary` are transport positions. When OpenAI drops the 5h limit the weekly window
    moves into `primary` with `secondary: null` (seen live on a prolite plan)."""
    wins = [w for w in ((bucket or {}).get("primary"), (bucket or {}).get("secondary"))
            if isinstance(w, dict) and w.get("usedPercent") is not None]
    known = [w for w in wins if w.get("windowDurationMins")]
    unknown = [w for w in wins if not w.get("windowDurationMins")]
    long_ = [w for w in known if w["windowDurationMins"] >= DAY_MINS]
    short_ = [w for w in known if w["windowDurationMins"] < DAY_MINS]
    wk = min(long_, key=lambda w: abs(w["windowDurationMins"] - WEEK_MINS)) if long_ else None
    sh = min(short_, key=lambda w: w["windowDurationMins"]) if short_ else None
    # durations are nullable. Two unlabelled windows: historic order (short, then weekly). One:
    # it is the weekly, the window that persists when the short limit is removed.
    if len(unknown) == 2: sh, wk = unknown[0], unknown[1]
    elif unknown:
        if wk is None: wk = unknown[0]
        elif sh is None: sh = unknown[0]
    return sh, wk

def _codex_scoped(buckets, want):
    """The model bucket to put in the device's third column, or (None, ''). Off unless asked."""
    want = (want or "").strip().lower()
    if not want: return None, ""
    for bid, b in sorted((buckets or {}).items()):
        if bid == CODEX_MAIN_BUCKET or not isinstance(b, dict): continue
        name = b.get("limitName") or ""
        if want in bid.lower() or want in name.lower():
            sh, wk = codex_windows(b)
            return (wk or sh), (name.split("-")[-1] or bid).strip().upper()[:7]
    return None, ""

def codex_account_from_rpc(slot, alias, home, raw, scoped=""):
    """Map one home's raw read ({account, limits, driver_err}) into the standard account record
    (the shape cswap_accounts_from_json documents), plus provider/slot/home/blocked/plan/buckets."""
    account_reply, limits_reply = raw.get("account"), raw.get("limits")
    auth, err = codex_status(account_reply, limits_reply, raw.get("driver_err") or "")
    acct = ((account_reply or {}).get("result") or {}).get("account") or {}
    res = (limits_reply or {}).get("result") or {}
    buckets = res.get("rateLimitsByLimitId") or {}
    main = buckets.get(CODEX_MAIN_BUCKET) or res.get("rateLimits") or {}
    sh, wk = codex_windows(main)
    # -1 means "not known", never 0 (the same rule the cswap mapper enforces)
    u = {"s": _cpct(sh), "w": _cpct(wk), "sr": "", "wr": "", "f": -1, "fl": ""}
    resets = {"session": _codex_iso(sh.get("resetsAt")) if sh else None,
              "week": _codex_iso(wk.get("resetsAt")) if wk else None}
    if resets["session"]: u["sr"] = _clock(_parse(resets["session"]))
    if resets["week"]:
        d = _parse(resets["week"]); u["wr"] = "%s %d %s" % (d.strftime("%b"), d.day, _clock_short(d))
    sw, tag = _codex_scoped(buckets, scoped)
    if sw: u["f"], u["fl"] = _cpct(sw), tag
    if auth == "ok" and not err and u["s"] < 0 and u["w"] < 0: err = "no_windows"
    email = acct.get("email") or ""
    return {"provider": "codex", "key": "codex:%s" % slot, "slot": slot, "home": home,
            "label": _label(alias, email, "") if (alias or email) else "CODEX",
            "email": email, "active": True, "disabled": False, "u": u,
            "stale": not (auth == "ok" and not err), "resets": resets, "auth": auth,
            "age": 0, "err": err, "ident": res.get("accountId") or "",
            "blocked": bool(res.get("ordinaryUsageAllowed") is False or main.get("rateLimitReachedType")),
            "plan": main.get("planType") or acct.get("planType") or "",
            "buckets": [{"id": bid, "name": b.get("limitName") or "",
                         "windows": [{"mins": w.get("windowDurationMins"), "pct": _cpct(w),
                                      "resets": _codex_iso(w.get("resetsAt"))}
                                     for w in (b.get("primary"), b.get("secondary")) if isinstance(w, dict)]}
                        for bid, b in sorted(buckets.items()) if isinstance(b, dict)]}

def codex_with_last_good(rec, good, now):
    """A failed read keeps SHOWING the last good numbers, as cswap's lastGoodUsage does; only
    the status fields come from the failed read. `stale` stays set, so nothing downstream
    (notifier, fleet) mistakes remembered numbers for a fresh reading."""
    if not rec["stale"]:
        rec["good_at"] = now; return rec
    if good:
        for k in ("u", "resets", "plan", "buckets", "ident", "blocked"): rec[k] = good[k]
        if not rec["email"]: rec["email"], rec["label"] = good["email"], good["label"]
        rec["good_at"] = good.get("good_at", now); rec["age"] = int(now - rec["good_at"])
    return rec

def _codex_reap(p):
    """Closing stdin makes the app-server exit by itself (30ms, rc 0). The kill is the fallback
    for a server that is hung on a backend call, which Codex itself never abandons."""
    try: p.stdin.close()
    except Exception: pass
    try: p.wait(2)
    except Exception:
        try:
            if os.name == "nt": p.kill()
            else: os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try: p.kill()
            except Exception: pass
        try: p.wait(5)
        except Exception: pass
    try: p.stdout.close()
    except Exception: pass

def codex_rpc_read(exe, home, deadline_s=20.0):
    """One short-lived `codex app-server` for one CODEX_HOME. Returns
    {account: reply|None, limits: reply|None, driver_err: ''|'timeout'|'unavailable'} where a
    reply is the whole JSON-RPC message. Never raises, always reaps the child. ONE absolute
    deadline covers spawn, handshake and both reads. `exe` may be a list (tests run a fake)."""
    out = {"account": None, "limits": None, "driver_err": ""}
    end = time.monotonic() + deadline_s
    env = dict(os.environ); env["CODEX_HOME"] = home
    kw = ({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt"
          else {"start_new_session": True})
    try:
        p = subprocess.Popen((exe if isinstance(exe, list) else [exe]) + ["app-server", "--disable", "plugins"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                             cwd=tempfile.gettempdir(), **kw)
    except Exception:
        out["driver_err"] = "unavailable"; return out
    replies = {}; cv = threading.Condition(); st = {"eof": False}
    def reader():
        try:
            for line in p.stdout:
                try: o = json.loads(line)
                except ValueError: continue          # log chatter, partial lines
                # anything without an id we sent is a notification (remoteControl/status/changed ...)
                if isinstance(o, dict) and "id" in o and ("result" in o or "error" in o):
                    with cv: replies[o["id"]] = o; cv.notify_all()
        except Exception: pass
        finally:
            with cv: st["eof"] = True; cv.notify_all()
    threading.Thread(target=reader, daemon=True).start()
    def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
    def wait(ids):
        with cv: cv.wait_for(lambda: all(i in replies for i in ids) or st["eof"],
                             max(0.0, end - time.monotonic()))
        return all(i in replies for i in ids)
    try:
        send({"method": "initialize", "id": 1, "params": {"clientInfo": {
              "name": "claudetv", "title": "ClaudeTV collector", "version": "1"}}})
        if wait([1]):
            send({"method": "initialized"})
            send({"method": "account/read", "id": 2, "params": {"refreshToken": False}})
            # the flag skips a second backend lookup; supportsLunaReserve is deliberately never
            # sent (Codex source: passive usage readers must not opt in)
            send({"method": "account/rateLimits/read", "id": 3,
                  "params": {"excludeResetCreditDetails": True}})
            wait([2, 3])
    except Exception: pass
    out["account"], out["limits"] = replies.get(2), replies.get(3)
    if out["limits"] is None: out["driver_err"] = "unavailable" if st["eof"] else "timeout"
    _codex_reap(p)
    return out

def codex_poll(exe, homes, deadline_s, reader=None):
    """Read every home CONCURRENTLY: a poll costs the slowest single read, and a hung backend
    costs one deadline rather than one per account. Results come back in `homes` order."""
    reader = reader or codex_rpc_read
    res = [None] * len(homes)
    def one(i, path):
        try: res[i] = reader(exe, path, deadline_s)
        except Exception: res[i] = None
    ths = [threading.Thread(target=one, args=(i, h[2]), daemon=True) for i, h in enumerate(homes)]
    for t in ths: t.start()
    for t in ths: t.join(deadline_s + 10)
    return [r or {"account": None, "limits": None, "driver_err": "timeout"} for r in res]

# ---------- in-app firmware updater ----------
# The collector already reaches both GitHub and the device, and the stock ESP8266HTTPUpdateServer
# at /update takes a plain multipart POST — the same thing `curl -F firmware=@...` does. So the
# whole download-and-flash dance can happen from the terminal with one button, and nobody needs
# a laptop, a release page and a curl incantation to take a firmware update.
GITHUB_RELEASES = "https://api.github.com/repos/latticelabs-au/ClaudeTV/releases/latest"
FW_MIN, FW_MAX = 200_000, 1_048_576      # sanity bounds for an ESP8266 4M1M image

def parse_release(doc):
    """Pull the generic firmware image out of a GitHub release payload."""
    doc = doc or {}
    asset = next((a for a in (doc.get("assets") or [])
                  if (a.get("name") or "").endswith("-generic.bin")), None) or {}
    return {"tag": (doc.get("tag_name") or "").strip(),
            "name": asset.get("name", ""), "url": asset.get("browser_download_url", ""),
            "size": int(asset.get("size") or 0),
            "notes": (doc.get("body") or "")[:4000],
            "published": doc.get("published_at") or ""}

def ver_tuple(v):
    """'v5.0' / '5.0.1' -> (5, 0, 1). Unknown sorts lowest so it never looks newer."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else ()

def update_available(current, latest):
    """True only when BOTH versions are known and latest is genuinely newer. An unreachable
    device reports no version, and must not be nagged about an update we cannot justify."""
    c, l = ver_tuple(current), ver_tuple(latest)
    return bool(c) and bool(l) and l > c

def check_image(data):
    """Refuse to push anything that is not plausibly an ESP8266 image. A GitHub outage that
    serves an HTML error page must never reach the device's flash."""
    if not data or len(data) < FW_MIN or len(data) > FW_MAX:
        raise ValueError("firmware image is %d bytes, expected %d..%d" % (len(data or b""), FW_MIN, FW_MAX))
    if data[0] != 0xE9:                  # ESP image magic
        raise ValueError("not an ESP8266 firmware image (bad magic 0x%02X)" % data[0])
    return True

def _multipart(field, filename, data):
    """Minimal multipart/form-data body — the shape ESP8266HTTPUpdateServer parses."""
    b = "----ClaudeTV" + secrets.token_hex(8)
    head = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n' % (b, field, filename))
    return ("multipart/form-data; boundary=%s" % b,
            head.encode() + data + ("\r\n--%s--\r\n" % b).encode())

def device_url():
    return (CONFIG.get("DEVICE_URL") or "").rstrip("/")

def device_info():
    """The device's own /state — firmware version, connection, which account it is showing."""
    with urllib.request.urlopen(device_url() + "/state", timeout=8) as r:
        return json.loads(r.read().decode())

_release = {"at": 0, "rel": None}
def latest_release(force=False):
    if not force and _release["rel"] and time.time() - _release["at"] < 3600:
        return _release["rel"]
    req = urllib.request.Request(GITHUB_RELEASES, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "ClaudeTV"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rel = parse_release(json.loads(r.read().decode()))
    _release.update({"at": time.time(), "rel": rel})
    return rel

# Live progress for the terminal to poll. Flashing is a one-at-a-time, hard-to-undo action.
_update = {"state": "idle", "msg": "", "at": 0, "device_ver": "", "latest": ""}

def update_status():
    st = dict(_update)
    rel = _release["rel"] or {}
    st["latest"] = rel.get("tag", "")
    st["notes"] = rel.get("notes", "")
    st["can_update"] = bool(rel.get("url")) and update_available(st.get("device_ver", ""), st["latest"])
    st["device_url"] = device_url()
    return st

def update_check():
    """Refresh both sides of the comparison: what the device runs, what GitHub offers."""
    try:
        _update["device_ver"] = str(device_info().get("ver", ""))
    except Exception as e:
        _update["device_ver"] = ""
        _update["msg"] = "device unreachable at %s (%s)" % (device_url(), str(e)[:60])
    try:
        latest_release(force=True)
        if _update["device_ver"]: _update["msg"] = ""
    except Exception as e:
        _update["msg"] = "release check failed: %s" % str(e)[:80]
    st = update_status()
    # cached so the hot /usage path never recomputes or reaches the network
    _update["can_update"] = st["can_update"]
    _update["latest"] = st["latest"]
    return st

def _do_update():
    def stage(state, msg=""):
        _update.update({"state": state, "msg": msg, "at": int(time.time())})
        print("[%s] update: %s %s" % (time.strftime("%H:%M:%S"), state, msg))
    try:
        rel = latest_release()
        if not rel.get("url"): raise RuntimeError("no firmware asset in the latest release")
        stage("downloading", rel["name"])
        req = urllib.request.Request(rel["url"], headers={"User-Agent": "ClaudeTV"})
        with urllib.request.urlopen(req, timeout=120) as r: data = r.read()
        check_image(data)
        stage("flashing", "%s (%d KB) -> %s" % (rel["name"], len(data) // 1024, device_url()))
        ctype, body = _multipart("firmware", rel["name"], data)
        req = urllib.request.Request(device_url() + "/update", data=body,
                                     headers={"Content-Type": ctype, "User-Agent": "ClaudeTV"})
        with urllib.request.urlopen(req, timeout=240) as r: r.read()
        stage("rebooting", "device is restarting on %s" % rel["tag"])
        # the ESP reboots straight after a successful flash; wait for it to answer again
        for _ in range(30):
            time.sleep(4)
            try:
                _update["device_ver"] = str(device_info().get("ver", ""))
                stage("done", "device now runs v%s" % _update["device_ver"]); return
            except Exception: pass
        stage("done", "flashed; the device has not answered yet — give it a moment")
    except Exception as e:
        stage("failed", str(e)[:160])

def update_start():
    if _update["state"] in ("downloading", "flashing", "rebooting"):
        return {"ok": 0, "msg": "an update is already running"}
    _update.update({"state": "starting", "msg": "", "at": int(time.time())})
    threading.Thread(target=_do_update, daemon=True).start()
    return {"ok": 1}

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
        try:
            with open(NOTIFY_STATE_PATH, encoding="utf-8") as f: _notify_state = json.load(f)
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
            with open(RESET_LOG_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line: _reset_log.append(json.loads(line))
            _reset_log = _reset_log[-30:]
        except Exception: _reset_log = []
    return _reset_log

def _newer(a, b):
    """Of two per-window state dicts, the one whose tracked resets_at is later."""
    if not isinstance(a, dict): return b
    if not isinstance(b, dict): return a
    ra, rb = _iso_dt(a.get("ra")), _iso_dt(b.get("ra"))
    if ra and rb: return a if ra >= rb else b
    return a if ra else b

def migrate_notify_state(accounts):
    """Re-home reset state onto stable account keys. Handles both historical layouts:

      * the single-account build's flat {'session':…, 'week':…}  -> the first account's key
      * v5.0's label-keyed namespaces                            -> that account's key

    Label keys are the reason this exists: cswap omits `alias` when unset, so one account could
    accumulate two namespaces (e.g. 'personal' and the email-derived 'varma.ad') with divergent
    history. Those are MERGED per window, keeping the entry that tracked the later resets_at, so
    the surviving baseline is the freshest one rather than an arbitrary winner. Idempotent."""
    st = _load_notify_state()
    if not accounts: return False
    keys = {notify_key(r) for r in accounts}
    changed = False

    legacy = {k: st.pop(k) for k in ("session", "week") if isinstance(st.get(k), dict)}
    if legacy:
        primary = notify_key(accounts[0])
        for kind, val in legacy.items():
            st.setdefault(primary, {})[kind] = _newer(st.get(primary, {}).get(kind), val)
        changed = True
        print("[notify] migrated single-account reset state onto '%s'" % primary)

    for rec in accounts:
        key = notify_key(rec)
        # every alias this account could have presented itself as
        aliases = {rec["label"].lower(), (rec.get("email") or "").split("@")[0][:LABEL_MAX].lower()}
        for old in aliases - keys:
            src = st.pop(old, None)
            if not isinstance(src, dict): continue
            dst = st.setdefault(key, {})
            for kind, val in src.items():
                dst[kind] = _newer(dst.get(kind), val)
            changed = True
            print("[notify] merged reset state '%s' -> '%s'" % (old, key))

    if changed: _save_notify_state()
    return changed

def _log_reset(window, cls, detail, acct=""):
    """Append-only record of every reset — expected rollovers AND Anthropic 'gifts'."""
    entry = {"at": _now_utc().isoformat(timespec="seconds"), "acct": acct,
             "window": window, "class": cls, "detail": detail}
    log = _load_reset_log(); log.append(entry); del log[:-30]
    try:
        with open(RESET_LOG_PATH, "a", encoding="utf-8") as f: f.write(json.dumps(entry) + "\n")
    except Exception as e: print("[notify] reset-log write failed: %s" % e)
    print("[%s] RESET %s%s (%s): %s" % (time.strftime("%H:%M:%S"), (acct + " ") if acct else "",
                                        window, cls, detail))

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

def notify_check(u, resets, acct="", label=""):
    """Detect + log usage-window resets (see the section header), then notify per the toggles.
    Per window: session=s / resets_at.five_hour; week=(w OR f) / resets_at.seven_day. Baselines
    silently on first sight; fires once per reset. Never breaks the poller.

    State is namespaced per account. That isolation is load-bearing: with one shared namespace,
    two accounts polled in turn read as one account whose usage swings wildly, and every swap
    logs a phantom reset (the single-account build did exactly this when the credential file
    behind it changed account). A new account key simply baselines and stays quiet."""
    try:
        root = _load_notify_state()
        st = root.setdefault(acct, {}); changed = False; now = _now_utc()
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
                shown = label or acct                   # humans see the label, state uses the key
                _log_reset(kind, cls, _reset_detail(kind, prev, u), shown)
                if _should_notify(kind, prev) and _channels():
                    title, body = _reset_message(kind, u, cls, kind == "session" and _was_maxed(prev))
                    if shown: title = "[%s] %s" % (shown, title)
                    threading.Thread(target=_dispatch, args=(title, body, _channels(), kind + "_reset"),
                                     daemon=True).start()
        if changed: _save_notify_state()
    except Exception as e:
        print("[notify] check error: %s" % e)

def notify_test(channel):
    with _lock: u = dict(_accounts[0]["u"]) if _accounts else {}
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
            # nested per account: {acct: {session: {...}, week: {...}}}
            "tracking": {a: v for a, v in st.items() if isinstance(v, dict)},
            "recent_resets": _load_reset_log()[-10:], "last_sent": _notify_last}

def fetch_accounts():
    """Read every account from cswap — the single source, for one account or twelve.

    There is deliberately no second code path. ClaudeTV does no OAuth of its own at runtime, so
    there is no keeper that could rotate a token family cswap also owns, and one account behaves
    exactly like many. When cswap is missing or empty this raises: the caller keeps serving
    last-good and the dashboards show a setup state, which is honest rather than a silent
    half-working fallback."""
    global _source_err
    if not cswap_bin():
        _source_err = "claude-swap is not installed on this host"
        raise RuntimeError(_source_err)
    accts = cswap_accounts()
    if not accts:
        _source_err = "claude-swap has no accounts yet (run: cswap add --alias <name>)"
        raise RuntimeError(_source_err)
    if _source_err: print("[%s] accounts: cswap (%d)" % (time.strftime("%H:%M:%S"), len(accts)))
    _source_err = ""
    return accts

def _auth_transitions(accts):
    """Edge-triggered per-account dead/recovered alerts: one per account per outage episode."""
    for rec in accts:
        dead, was = rec["auth"] == "dead", _alerted.get(rec["key"], False)
        if dead and not was:
            _alerted[rec["key"]] = True
            _auth_alert("dead", "Anthropic rejected the Claude login for %s%s. That account shows "
                        "LOGIN EXPIRED on the display until you log in again (cswap: log in with "
                        "that account and re-run `cswap add`; native: "
                        "python3 claude_usage_server.py --login)."
                        % (rec["label"], (" (%s)" % rec["email"]) if rec["email"] else ""))
        elif not dead and was:
            _alerted[rec["key"]] = False
            _auth_alert("recovered", "%s is accepted again; the display is back to live data."
                        % rec["label"])

def poller():
    global _accounts, _usage_ts, _usage_err, _wx, _wx_err, _migrated, _force_poll, _fleet
    next_u = 0.0; backoff = int(CONFIG["USAGE_EVERY"]); next_w = 0.0; next_upd = 30.0
    while True:
        now = time.time()
        if now >= next_u or _force_poll:
            _force_poll = False
            try:
                accts = fetch_accounts()
                with _lock:
                    _accounts = accts; _usage_ts = int(now); _usage_err = ""
                _auth_transitions(accts)
                if not _migrated and accts:             # first poll: re-home state onto stable keys
                    _migrated = True
                    migrate_notify_state(accts)
                backoff = int(CONFIG["USAGE_EVERY"]); next_u = now + backoff
                for rec in accts:                       # detect/log/notify resets (never raises)
                    # A stale/partial reading is skipped outright rather than baselined: writing
                    # it would make the NEXT good poll look like a jump back up.
                    if notifiable(rec):
                        notify_check(rec["u"], rec["resets"], acct=notify_key(rec),
                                     label=rec["label"])
                # "every account is out" is the only state that actually blocks you; one account
                # capping just makes cswap switch. Checked after the per-account pass so the
                # verdict uses this poll's numbers.
                _f = fleet_check(accts, switch_policy())
                with _lock: _fleet = _f
            except Exception as e:
                # cswap owns credentials and their upkeep, so a failure here is cswap being
                # absent, empty or briefly unhappy — never an auth verdict. Per-account auth
                # comes from usageStatus alone, so a hiccup can no longer flip the device to
                # LOGIN EXPIRED. Keep last-good and retry.
                with _lock: _usage_err = str(e)[:90]
                next_u = now + 30
        if now >= next_upd:
            try: update_check()
            except Exception as e: print("[update] check error: %s" % str(e)[:80])
            next_upd = now + max(3600, int(CONFIG.get("UPDATE_EVERY") or 21600))
        if now >= next_w:
            try:
                w = fetch_weather()
                with _lock: _wx = w; _wx_err = ""
                next_w = now + int(CONFIG["WEATHER_EVERY"])
            except Exception as e:
                next_w = now + 120
                with _lock: _wx_err = str(e)[:50]
        time.sleep(2)

def device_json(primary=""):
    with _lock: accts, ts, err, wx = list(_accounts), _usage_ts, _usage_err, _wx
    st = usage_wire(accts, wx, primary)
    # collector-side staleness (when we last polled) beats a per-account cache age here: it is
    # what the device's "stale Nm" readout has always meant.
    st["age"] = (int(time.time()) - ts) if ts else -1
    # a firmware update the user can take from the terminal in one click; the device just
    # shows a small marker so it is discoverable without opening anything.
    if _update.get("can_update"): st["up"] = 1
    if err: st["err"] = err
    return st

def full_state():
    with _lock: accts, ts, err, wx, wxe = list(_accounts), _usage_ts, _usage_err, _wx, _wx_err
    u = accts[0]["u"] if accts else None
    return {"service": {"uptime_s": int(time.time() - START_TS), "port": PORT},
            "fleet": {**_fleet, "policy": switch_policy()},
            "accounts": {"ready": bool(accts), "source_err": _source_err, "cswap": cswap_bin(),
                         "cswap_ver": cswap_version(), "filter": CONFIG.get("CSWAP_ACCOUNTS", ""),
                         "list": [{"key": a["key"], "label": a["label"], "email": a["email"],
                                   "active": a["active"], "auth": a["auth"], "age": a["age"],
                                   "err": a["err"], "stale": a.get("stale", False),
                                   "disabled": a.get("disabled", False), **a["u"]} for a in accts]},
            "usage": {"ok": 1 if u else 0, "age": (int(time.time()) - ts) if ts else -1, "err": err, **(u or {})},
            "update": update_status(),
            "weather": (wx or {}), "weather_err": wxe, "config": {k: CONFIG[k] for k in EDITABLE},
            "notify": notify_status()}

def restart_later():
    def go(): time.sleep(0.5); os._exit(0)
    threading.Thread(target=go, daemon=True).start()

TERMINAL = """<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
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
button.ghost{background:#1c2331;color:#e6e9ef;border:1px solid var(--line)}button:disabled{opacity:.45;cursor:not-allowed}.muted{color:var(--gray);font-size:12px}
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
<label style="margin-top:6px" for=DEVICE_URL>ClaudeTV device URL</label><input id=DEVICE_URL>
<button style="background:#39c3cd;color:#06222a;font-weight:700;margin-top:8px" onclick="window.open(devUrl||'http://claudetv.local','_blank')">Open ClaudeTV device &#8599;</button>
<div class=grid style=margin-top:8px><button class=ghost onclick=restart()>Restart service</button><button class=ghost onclick=load()>Refresh</button></div></div>

<div class=card><h2>Device firmware</h2>
<div class=row><span>Installed</span><span class=pill id=fwnow>--</span></div>
<div class=row><span>Latest release</span><span class=pill id=fwnew>--</span></div>
<div class=muted id=fwmsg></div>
<div class=grid style=margin-top:8px><button class=ghost onclick=fwcheck()>Check for updates</button>
<button id=fwbtn onclick=fwflash() disabled>Update device</button></div>
<div class=muted style="margin-top:8px">Downloads the release image and flashes it over your LAN. The device reboots itself; nothing to download or plug in.</div></div>



<div class=card><h2>Accounts</h2>
<div class=row><span>Source</span><span class=pill id=src>--</span></div>
<div class=muted id=srcerr></div>
<div class=row id=fleetrow style="display:none"><span>Quota</span><span class=pill id=fleet>--</span></div>
<div class=muted id=fleetmeta></div>
<div id=accts style="margin-top:6px"></div>
<div id=cswapadd class=muted style="display:none;margin-top:8px"></div>
<div class=grid style=margin-top:8px><button class=ghost onclick=poll()>Re-read accounts</button>
<button class=ghost onclick="document.getElementById('acchelp').style.display=''">How to add an account</button></div>
<div id=acchelp class=muted style="display:none;margin-top:8px;line-height:1.6">
Accounts come from <b>claude-swap</b>. Install it on this host:<br>
<code>pipx install claude-swap</code> &nbsp;or&nbsp; <code>uv tool install claude-swap</code><br><br>
Then, <b>for each account</b>: log in to Claude Code on this box as that account and run<br>
<code>cswap add --alias &lt;name&gt;</code><br>
The alias becomes the label on the display (first 8 characters).<br><br>
<b>Log in separately on every machine.</b> Refresh tokens rotate, so if two machines hold the
same login the first to refresh invalidates the other and that account gets quarantined.
</div>
<label style="margin-top:10px" for=CSWAP_ACCOUNTS>Show only these accounts (blank = all; comma list of alias/email, sets order)</label>
<input id=CSWAP_ACCOUNTS placeholder="e.g. work,personal">
<div class=row><span class=muted id=uerr></span><span class=muted id=age></span></div>
<div class=row><span id=wx class=muted></span></div></div>

<div class=card><h2>Weather &amp; timezone</h2>
<label for=citySearch>Search a city (sets location + timezone automatically)</label>
<input id=citySearch placeholder="e.g. Melbourne" autocomplete=off>
<div id=geoResults style="margin-top:6px"></div>
<div class=muted style="margin-top:8px">Current: <b id=CITY_disp>--</b> <span id=geoMeta></span></div>
<input type=hidden id=CITY><input type=hidden id=LAT><input type=hidden id=LON><input type=hidden id=TZ>
<label style="margin-top:8px" for=WEATHER_EVERY>Weather refresh (s)</label><input id=WEATHER_EVERY></div>

<div class=card><h2>Collector</h2>
<div class=grid><div><label for=USAGE_EVERY>Usage poll (s)</label><input id=USAGE_EVERY></div><div><label for=PORT>Port</label><input id=PORT></div></div>
<label for=MAXED_THRESHOLD>Blocked-at threshold %% (blank = follow cswap's autoswitch.threshold)</label><input id=MAXED_THRESHOLD placeholder="from cswap">
<div class=muted style="margin-top:8px">Credentials and token upkeep belong to <b>claude-swap</b>; ClaudeTV never holds a Claude token.</div></div>

<div class=card><h2>Reset notifications</h2>
<div class=muted>Get pinged when your Claude usage window rolls over to a fresh quota (the reset Anthropic only posts on X).</div>
<div class=row style="margin-top:8px"><label for=NOTIFY_SESSION_RESET style="display:inline">Session reset (5h) &mdash; every reset</label><input type=checkbox id=NOTIFY_SESSION_RESET></div>
<div class=row><label class=muted for=NOTIFY_SESSION_MAXED style="display:inline">&nbsp;&nbsp;&#8627; only when the session maxed out (hit its cap)</label><input type=checkbox id=NOTIFY_SESSION_MAXED></div>
<div class=row><label for=NOTIFY_WEEK_RESET style="display:inline">Week reset (7d)</label><input type=checkbox id=NOTIFY_WEEK_RESET></div>
<div class=row><label for=NOTIFY_AUTH style="display:inline">Auth outage / failover alerts</label><input type=checkbox id=NOTIFY_AUTH></div>
<div class=muted id=nstat></div>
<label style="margin-top:10px" for=NOTIFY_DISCORD_WEBHOOK>Discord webhook URL</label>
<div style="display:flex;gap:8px"><input id=NOTIFY_DISCORD_WEBHOOK placeholder="https://discord.com/api/webhooks/…" style="flex:1"><button class=ghost style="width:auto" onclick="ntest('discord')">Test</button></div>
<label style="margin-top:8px" for=NOTIFY_SLACK_WEBHOOK>Slack webhook URL</label>
<div style="display:flex;gap:8px"><input id=NOTIFY_SLACK_WEBHOOK placeholder="https://hooks.slack.com/services/…" style="flex:1"><button class=ghost style="width:auto" onclick="ntest('slack')">Test</button></div>
<div class=row style="margin-top:12px"><label for=NOTIFY_EMAIL style="display:inline">Email alerts (SMTP)</label><input type=checkbox id=NOTIFY_EMAIL></div>
<div class=grid><div><label for=SMTP_HOST>SMTP host</label><input id=SMTP_HOST placeholder=smtp.gmail.com></div><div><label for=SMTP_PORT>Port</label><input id=SMTP_PORT></div></div>
<div class=grid><div><label for=SMTP_SECURITY>Security</label><input id=SMTP_SECURITY placeholder="starttls · ssl · none"></div><div><label for=SMTP_FROM>From address</label><input id=SMTP_FROM placeholder=you@example.com></div></div>
<div class=grid><div><label for=SMTP_USER>SMTP user</label><input id=SMTP_USER></div><div><label for=SMTP_PASS>SMTP password</label><input id=SMTP_PASS type=password></div></div>
<label for=NOTIFY_EMAIL_TO>Send alerts to</label>
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
 const u=s.usage,A=s.accounts||{ready:false,list:[]},cs=!!A.ready;
 pill(src,cs?'ok':'bad',cs?('claude-swap · '+A.list.length+(A.list.length==1?' account':' accounts')):'setup needed');
 srcerr.textContent=cs?((A.cswap_ver||'cswap')+' · '+A.cswap):('⚠ '+(A.source_err||'claude-swap not ready'));
 // fleet verdict: one account capping is normal (cswap switches); ALL of them is a block
 const U=s.update||{};
 pill(fwnow,U.device_ver?'ok':'bad',U.device_ver?('v'+U.device_ver):'device unreachable');
 pill(fwnew,U.can_update?'warn':'ok',U.latest||'--');
 // while a flash runs, the state field narrates it; otherwise show any error
 const busy=['starting','downloading','flashing','rebooting'].indexOf(U.state)>=0;
 fwmsg.textContent=busy?(U.state+'… '+(U.msg||'')):(U.msg||(U.can_update?('update '+U.latest+' available'):'up to date'));
 fwbtn.disabled=busy||!U.can_update;
 fwbtn.textContent=busy?'updating…':('Update device'+(U.can_update?(' to '+U.latest):''));
 if(!busy&&fwpoll){clearInterval(fwpoll);fwpoll=null;}
 const F=s.fleet||{},P=F.policy||{},B=F.benched||[];
 fleetrow.style.display=(A.list.length?'':'none');
 pill(fleet,F.exhausted?'bad':'ok',F.exhausted?(B.length?'NO ACCOUNT IN ROTATION':'ALL ACCOUNTS OUT'):((F.headroom||[]).length+' with room'));
 fleetmeta.textContent=(F.exhausted&&B.length?(B.map(b=>b.label+' has room ('+b.pct+'%) but is '+b.why
   +' - cswap enable '+b.label.toLowerCase()).join('; ')+' · '):'')
   +(P.threshold?('blocked at '+P.threshold+'% binding · cswap '+P.strategy
   +' · hysteresis '+P.hysteresis+'pp · cooldown '+P.cooldown+'s'+(P.model?(' · model '+P.model):'')):'');
 // no cswap on this box -> tell them exactly how to get multi-account, inline
 cswapadd.style.display=cs?'none':'';
 cswapadd.innerHTML=cs?'':'Want more than one account? Install <b>claude-swap</b> on this host, then add each login.';
 accts.innerHTML=(A.list||[]).map(a=>{const dead=a.auth=='dead';
   const pc=v=>(v==null||v<0)?'--':v+'%';   // -1 = no reading this poll
   const f=a.f>=0?(' · '+(a.fl||'F')+' <b>'+pc(a.f)+'</b>'):'';
   return '<div style="border-top:1px solid var(--line);padding:8px 0">'
    +'<div class=row style=margin:0><span><b>'+a.label+'</b>'+(a.active?' <span class=muted>· active</span>':'')
      +'</span><span class="pill '+(dead?'bad':'ok')+'">'+(dead?'LOGIN EXPIRED':'ok')+'</span></div>'
    +'<div class=row style="margin:2px 0"><span class=muted>'+(a.email||'')+'</span>'
      +'<span>'+(a.stale?'<span style=color:#f0ad36>stale </span>':'')
        +'S <b>'+pc(a.s)+'</b> · W <b>'+pc(a.w)+'</b>'+f+'</span></div>'
    +'<div class=row style=margin:0><span class=muted>'+(a.sr?('resets '+a.sr):'idle')+(a.wr?(' · '+a.wr):'')
      +'</span><span class=muted>'+(a.err||(a.age?a.age+'s':''))+'</span></div></div>';}).join('')
   ||'<div class=muted>no accounts — run <code>cswap add</code>, or log in with --login</div>';
 uerr.textContent=u.err?('⚠ '+u.err):'';age.textContent=u.age>=0?('polled '+u.age+'s ago'):'';
 const w=s.weather;wx.textContent=w.city?(w.city+' '+w.wt+'°C '+w.wc+' · feels '+w.wfl+'° · '+w.wlo+'/'+w.whi+'° · rain '+w.wrain+'%'):'weather --';
 for(const k in s.config){const el=document.getElementById(k);if(el&&document.activeElement!==el){
   if(el.type=='checkbox')el.checked=(s.config[k]=='true');else el.value=s.config[k];}}
 const n=s.notify||{};
 // a saved secret shows the mask as its VALUE (looks filled = obviously saved); saveCfg skips it
 [['discord_set',NOTIFY_DISCORD_WEBHOOK],['slack_set',NOTIFY_SLACK_WEBHOOK],['smtp_pass_set',SMTP_PASS]].forEach(([k,el])=>{
   if(n[k]&&document.activeElement!==el&&!el.value)el.value='********';});
 nstat.textContent='Channels: '+((n.channels||[]).join(', ')||'none configured');
 const ls=n.last_sent||{};if(ls.event)nres.textContent='last: '+ls.event+' — '+Object.entries(ls.results||{}).map(([k,v])=>k+' '+v).join(', ');
 const rr=n.recent_resets||[];nlog.innerHTML=rr.length?('<b>Recent resets</b><br>'+rr.slice().reverse().map(e=>e.at.slice(0,16).replace('T',' ')+(e.acct?(' · '+e.acct):'')+' · '+e.window+' · '+(e.class=='gift'?'🎁 gift':'scheduled')+(e.detail?(' · '+e.detail):'')).join('<br>')):'';
 CITY_disp.textContent=s.config.CITY||'--';geoMeta.textContent=s.config.LAT?('· '+s.config.TZ):'';
 devUrl=s.config.DEVICE_URL||'';
}).catch(()=>{pill(svc,'bad','unreachable')})}
let geoT;
citySearch.oninput=function(){clearTimeout(geoT);const q=this.value.trim();if(q.length<2){geoResults.innerHTML='';return;}
 geoT=setTimeout(()=>{fetch('/api/geocode?q='+encodeURIComponent(q)).then(r=>r.json()).then(rs=>{geoResults.innerHTML='';
  rs.forEach(h=>{const b=document.createElement('button');b.className='ghost';b.style.marginBottom='4px';b.textContent=h.label;b.onclick=()=>pickCity(h);geoResults.appendChild(b);});});},350);};
function pickCity(h){geoResults.innerHTML='';citySearch.value='';pill(svc,'warn','applying…');
 fetch('/api/config?CITY='+encodeURIComponent(h.city)+'&LAT='+h.lat+'&LON='+h.lon+'&TZ='+encodeURIComponent(h.tz),{method:'POST'}).then(()=>setTimeout(load,3500));}
function saveCfg(){const ks=['CITY','LAT','LON','TZ','WEATHER_EVERY','DEVICE_URL','USAGE_EVERY','PORT','MAXED_THRESHOLD','CSWAP_ACCOUNTS',
  'SMTP_HOST','SMTP_PORT','SMTP_SECURITY','SMTP_FROM','SMTP_USER','NOTIFY_EMAIL_TO'];
 const parts=ks.map(k=>k+'='+encodeURIComponent(document.getElementById(k).value));
 ['NOTIFY_SESSION_RESET','NOTIFY_SESSION_MAXED','NOTIFY_WEEK_RESET','NOTIFY_AUTH','NOTIFY_EMAIL'].forEach(k=>parts.push(k+'='+(document.getElementById(k).checked?'true':'false')));
 ['NOTIFY_DISCORD_WEBHOOK','NOTIFY_SLACK_WEBHOOK','SMTP_PASS'].forEach(k=>{const v=document.getElementById(k).value.trim();if(v&&v!=='********')parts.push(k+'='+encodeURIComponent(v));});
 if(!confirm('Save config and restart the collector?'))return;
 fetch('/api/config?'+parts.join('&'),{method:'POST'}).then(()=>{pill(svc,'warn','restarting');setTimeout(load,3500)})}
function ntest(ch){nres.textContent='testing '+ch+'…';
 fetch('/api/notify-test?channel='+ch,{method:'POST'}).then(r=>r.json()).then(d=>{
  nres.textContent=Object.entries(d.results||{}).map(([k,v])=>k+': '+v).join(' · ')||'no channel configured';}).catch(()=>{nres.textContent='test failed'});}
function fwcheck(){fwmsg.textContent='checking…';fetch('/api/update?action=check',{method:'POST'}).then(load)}
function fwflash(){if(!confirm('Download the latest firmware and flash the device now?\\n\\nThe device reboots and is unavailable for about a minute.'))return;
 fwbtn.disabled=true;fetch('/api/update?action=flash',{method:'POST'}).then(()=>{fwpoll=setInterval(load,3000);load()})}
let fwpoll=null;
function poll(){pill(svc,'warn','re-reading…');fetch('/api/service?action=poll',{method:'POST'}).then(()=>setTimeout(()=>{pill(svc,'ok','running');load()},1500))}
function restart(){if(!confirm('Restart collector?'))return;fetch('/api/service?action=restart',{method:'POST'}).then(()=>{pill(svc,'warn','restarting');setTimeout(load,3500)})}
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
        path, q = self._q()
        # /usage?acct=<label|email> pins which account fills the flat keys, so a second device
        # can show a different account off the same collector.
        if path.startswith("/usage"): self._send(200, "application/json", json.dumps(device_json(q.get("acct", "")), separators=(",", ":")))
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
        elif path == "/api/service" and q.get("action") == "poll":
            globals()["_force_poll"] = True          # re-read accounts now (dashboard button)
            self._send(200, "application/json", '{"ok":1}')
        elif path == "/api/service" and q.get("action") == "refresh":
            self._send(200, "application/json", '{"ok":1}')
        elif path == "/api/update" and q.get("action") == "check":
            self._send(200, "application/json", json.dumps(update_check()))
        elif path == "/api/update" and q.get("action") == "flash":
            self._send(200, "application/json", json.dumps(update_start()))
        elif path == "/api/notify-test":
            self._send(200, "application/json", json.dumps({"results": notify_test(q.get("channel", "all"))}))
        else: self._send(404, "text/plain", "not found")


def oauth_login():
    """One-time ENROLLMENT helper for a headless box with no Claude Code.

    Mints a credential in Claude Code's own format and location so that `cswap add` can adopt it
    as a managed account. That is its whole job: cswap owns everything afterwards, including
    refreshing. Open the printed URL on ANY device, log in, paste the code back here.

    Run it once PER ACCOUNT, each followed by `cswap add --alias <name>`. Log in separately on
    every machine rather than copying credentials around: refresh tokens rotate, so a shared
    token family means the first machine to refresh invalidates the other's copy."""
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
    print("Logged in. Credential written to %s (0600)." % CRED_OWN)
    print("\nNow hand it to claude-swap, which manages it from here on:\n")
    print("    cswap add --alias <name>\n")
    print("Repeat this whole step for each additional account.")

if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        oauth_login(); raise SystemExit(0)
    if not cswap_bin():
        print("WARNING: claude-swap (cswap) is not installed — ClaudeTV reads all accounts from\n"
              "         it. Install it and add an account, then this starts serving:\n"
              "           pipx install claude-swap && cswap add --alias <name>")
    threading.Thread(target=poller, daemon=True).start()
    print("ClaudeTV collector + terminal on http://0.0.0.0:%d  (device -> /usage, terminal -> /)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
