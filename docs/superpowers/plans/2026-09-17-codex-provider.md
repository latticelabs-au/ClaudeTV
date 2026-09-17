# Codex Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show OpenAI Codex (ChatGPT-plan) usage, for one or several Codex accounts, on the same display and master terminal as Claude usage, without weakening any guarantee the Claude path has today.

**Architecture:** Claude stays on cswap, untouched. Codex becomes a second, optional account source: the collector launches the official `codex app-server` (JSON-RPC over stdio) once per `CODEX_HOME` directory, calls `account/rateLimits/read`, and maps the reply into the existing account record. Codex I/O runs on its own thread; every stateful pass (alerts, reset detection, fleet) stays on the existing poller thread, so notifier state remains single-threaded and a hung Codex can never delay Claude.

**Tech Stack:** Python 3 stdlib only (`subprocess`, `threading`, `json`, `re`, `signal`), stdlib `unittest`. No new dependencies. External binary: `codex` (codex-cli 0.154.0 verified).

**Spec:** `docs/superpowers/specs/2026-09-17-codex-provider-design.md`. Read it before starting; this plan argues from it.

## Global Constraints

- Stdlib only. The collector is one module, `host/claude_usage_server.py`; tests are one module, `host/test_collector.py`. Do not add files to `host/` other than those named here.
- The collector never opens, parses, copies or logs `auth.json`, and never performs or triggers a login. It may only `stat` the file.
- A number that is not known is `-1`, never `0`.
- Only a login that needs a human is `auth: "dead"`: no login in the home, or the usage endpoint answering HTTP 401, or HTTP 403 with a non-HTML body. HTTP 429 is never dead. Unknown errors are never dead.
- Codex is always launched as `app-server --disable plugins` (1 backend request instead of 7), with `excludeResetCreditDetails: true`, and `supportsLunaReserve` is never sent.
- Codex never times out a hung backend. Every read has one absolute deadline owned by the collector (`CODEX_TIMEOUT`, default 20 s) and the child is always reaped.
- `CODEX_EVERY` has a floor of 300 s. A failure never makes the source poll faster than that.
- Claude behaviour, Claude record keys, and the flat `/usage` keys do not change. The 88 existing tests stay green after every task.
- NEVER write an em dash character in any code, comment, string, doc or commit message. Restructure with a colon, comma, parentheses or two sentences. If you must touch an existing line that contains one, rewrite that line without it.
- Commit messages carry no AI attribution of any kind.
- Prefix shell commands with `rtk` (it passes unknown commands through unchanged).
- Test command, from `host/`: `rtk python -m unittest test_collector` (on the Ubuntu host use `python3`).

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `host/claude_usage_server.py` | modify | new section "accounts: codex source" placed directly after `usage_wire()` and before the `in-app firmware updater` section; small edits to config keys, `_auth_transitions`, `_reset_message`, `notify_check`, `notifiable`, `poller`, `device_json`, `full_state`, `usage_wire`, the `TERMINAL` HTML/JS, `do_POST`, `__main__` |
| `host/test_collector.py` | modify | Codex fixtures plus new test classes appended at the end of the file, before the `if __name__ == "__main__"` block if one exists |
| `host/.env.example` | modify | Codex keys |
| `host/install.sh` | modify | detect `codex`, print the add-account line |
| `README.md` | modify | "Codex accounts" section |
| `.gitignore` | modify | `**/auth.json` as defence in depth |

## Task 0: Branch and baseline

- [ ] **Step 1: Create the branch**

```bash
rtk git checkout -b feat/codex-provider
```

- [ ] **Step 2: Confirm the baseline**

Run from `host/`: `rtk python -m unittest test_collector`
Expected: `Ran 88 tests` and `OK`.

---

### Task 1: Codex status vocabulary

**Files:**
- Modify: `host/claude_usage_server.py` (insert the new section after `usage_wire()`, which ends with `return st`, and before the comment line `# ---------- in-app firmware updater ----------`)
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `codex_status(account_reply, limits_reply, driver_err="") -> (auth, err)` where `auth` is `"ok"` or `"dead"` and `err` is one of `""`, `login_required`, `login_expired`, `blocked_by_edge`, `rate_limited`, `unavailable`, `timeout`, `unknown`, `api_key`. A "reply" is a whole JSON-RPC message (`{"id":..,"result":{..}}` or `{"id":..,"error":{"code":..,"message":..}}`) or `None`. Test fixtures `cx_win`, `CODEX_LIMITS`, `CODEX_ACCT`, `cx_raw`, `cx_limits` used by later tasks.

- [ ] **Step 1: Write the failing test**

Append to `host/test_collector.py`:

```python
# ---------------------------------------------------------------- codex provider
def cx_win(pct, mins, at=1790220250):
    return {"usedPercent": pct, "windowDurationMins": mins, "resetsAt": at}


# Captured from a live `account/rateLimits/read` on 2026-09-17 (prolite plan). The weekly window
# sits in the PRIMARY slot and there is no short window at all: slots are not meanings.
CODEX_LIMITS = {"id": 3, "result": {
    "ordinaryUsageAllowed": True, "accountId": "acc-1",
    "rateLimits": {"limitId": "codex", "limitName": None, "primary": cx_win(19, 10080),
                   "secondary": None, "planType": "prolite", "rateLimitReachedType": None},
    "rateLimitsByLimitId": {
        "codex": {"limitId": "codex", "limitName": None, "primary": cx_win(19, 10080),
                  "secondary": None, "planType": "prolite", "rateLimitReachedType": None},
        "codex_bengalfox": {"limitId": "codex_bengalfox", "limitName": "GPT-5.3-Codex-Spark",
                            "primary": cx_win(3, 300, 1789641634), "secondary": cx_win(7, 10080),
                            "planType": "prolite"}}}}
CODEX_ACCT = {"id": 2, "result": {"account": {"type": "chatgpt", "email": "cosmo@example.com",
                                              "planType": "prolite"}, "requiresOpenaiAuth": True}}


def cx_raw(limits=CODEX_LIMITS, account=CODEX_ACCT, driver_err=""):
    return {"account": account, "limits": limits, "driver_err": driver_err}


def cx_limits(**main):
    """CODEX_LIMITS with the main bucket's fields overridden."""
    d = json.loads(json.dumps(CODEX_LIMITS))
    d["result"]["rateLimitsByLimitId"]["codex"].update(main)
    return d


def cx_err(code, message):
    return {"id": 3, "error": {"code": code, "message": message}}


CX_HTTP = ("failed to fetch codex rate limits: GET https://chatgpt.com/backend-api/wham/usage "
           "failed: %s; content-type=%s; body=x")


class TestCodexStatus(unittest.TestCase):
    """Only a login that needs a human is dead. Everything else keeps last-good and retries."""

    ROWS = [
        ("good reply", (CODEX_ACCT, CODEX_LIMITS, ""), ("ok", "")),
        ("no login in this home", ({"id": 2, "result": {"account": None}},
            cx_err(-32600, "codex account authentication required to read rate limits"), ""),
            ("dead", "login_required")),
        ("-32600 without an account reply", (None, cx_err(-32600, "x"), ""), ("dead", "login_required")),
        ("usage endpoint says 401", (CODEX_ACCT, cx_err(-32603, CX_HTTP % ("401 Unauthorized", "application/json")), ""),
            ("dead", "login_expired")),
        ("403 with a json body", (CODEX_ACCT, cx_err(-32603, CX_HTTP % ("403 Forbidden", "application/json")), ""),
            ("dead", "login_expired")),
        ("403 html is a cloudflare challenge", (CODEX_ACCT, cx_err(-32603, CX_HTTP % ("403 Forbidden", "text/html; charset=UTF-8")), ""),
            ("ok", "blocked_by_edge")),
        ("429 is never dead", (CODEX_ACCT, cx_err(-32603, CX_HTTP % ("429 Too Many Requests", "")), ""),
            ("ok", "rate_limited")),
        ("500", (CODEX_ACCT, cx_err(-32603, CX_HTTP % ("500 Internal Server Error", "")), ""), ("ok", "unavailable")),
        ("connection refused", (CODEX_ACCT, cx_err(-32603,
            "failed to fetch codex rate limits: error sending request for url (x)"), ""), ("ok", "unavailable")),
        ("undecodable body", (CODEX_ACCT, cx_err(-32603,
            "failed to fetch codex rate limits: Decode error for x: expected value"), ""), ("ok", "unavailable")),
        ("driver timeout", (CODEX_ACCT, None, "timeout"), ("ok", "timeout")),
        ("an error code we have never seen", (CODEX_ACCT, cx_err(-32099, "new thing"), ""), ("ok", "unknown")),
        ("api key login has no subscription quota", ({"id": 2, "result": {"account": {"type": "apiKey"}}},
            cx_err(-32600, "chatgpt authentication required to read rate limits"), ""), ("ok", "api_key")),
        ("nothing came back at all", (None, None, "unavailable"), ("ok", "unavailable")),
    ]

    def test_status_table(self):
        for name, args, want in self.ROWS:
            with self.subTest(name):
                self.assertEqual(srv.codex_status(*args), want)

    def test_a_429_in_the_body_text_of_a_500_is_not_rate_limiting(self):
        msg = CX_HTTP % ("500 Internal Server Error", "") + " upstream said 429"
        self.assertEqual(srv.codex_status(CODEX_ACCT, cx_err(-32603, msg), ""), ("ok", "unavailable"))
```

- [ ] **Step 2: Run test to verify it fails**

Run from `host/`: `rtk python -m unittest test_collector.TestCodexStatus`
Expected: FAIL with `AttributeError: module 'claude_usage_server' has no attribute 'codex_status'`.

- [ ] **Step 3: Write minimal implementation**

Insert into `host/claude_usage_server.py`, after `usage_wire()` and before the firmware updater section:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `rtk python -m unittest test_collector.TestCodexStatus`, then the full suite.
Expected: PASS; full suite `Ran 90 tests`, `OK`.

- [ ] **Step 5: Commit**

```bash
rtk git add host/claude_usage_server.py host/test_collector.py
rtk git commit -m "codex: status vocabulary (dead only when the usage endpoint rejects the login)"
```

---

### Task 2: Mapper and last-good merge

**Files:**
- Modify: `host/claude_usage_server.py` (same new section; put the constants at the top of the section, the functions after `codex_status`)
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: `codex_status` (Task 1); existing `_clock`, `_clock_short`, `_parse`, `_label`.
- Produces:
  - `codex_windows(bucket) -> (short_window_or_None, long_window_or_None)`
  - `codex_account_from_rpc(slot, alias, home, raw, scoped="") -> record`, where `raw` is `{"account": reply|None, "limits": reply|None, "driver_err": str}`. Record keys: everything `cswap_accounts_from_json` documents (`key, label, email, active, disabled, u{s,w,sr,wr,f,fl}, stale, resets{session,week}, auth, age, err`) plus `provider="codex"`, `slot`, `home`, `ident`, `blocked`, `plan`, `buckets`.
  - `codex_with_last_good(rec, good, now) -> rec` (adds `good_at`).

- [ ] **Step 1: Write the failing test**

Append to `host/test_collector.py`:

```python
class TestCodexMapping(TzPinned):
    def rec(self, raw=None, slot="default", alias="", scoped=""):
        return srv.codex_account_from_rpc(slot, alias, "/homes/x", raw or cx_raw(), scoped)

    def test_weekly_window_in_the_primary_slot_maps_to_week_not_session(self):
        u = self.rec()["u"]
        self.assertEqual((u["s"], u["w"]), (-1, 19))

    def test_a_missing_window_is_minus_one_never_zero(self):
        self.assertEqual(self.rec()["u"]["s"], -1)
        self.assertEqual(self.rec()["u"]["sr"], "")

    def test_a_good_read_is_fresh_and_ok(self):
        r = self.rec()
        self.assertEqual((r["stale"], r["auth"], r["err"], r["provider"]), (False, "ok", "", "codex"))

    def test_key_comes_from_the_slot_so_it_is_identical_on_a_failed_read(self):
        good, bad = self.rec(), self.rec(cx_raw(None, None, "timeout"))
        self.assertEqual(good["key"], "codex:default")
        self.assertEqual(bad["key"], good["key"])

    def test_label_prefers_the_alias_then_the_email_then_a_fixed_word(self):
        self.assertEqual(self.rec(alias="work", slot="work")["label"], "WORK")
        self.assertEqual(self.rec()["label"], "COSMO")
        self.assertEqual(self.rec(cx_raw(None, None, "timeout"))["label"], "CODEX")

    def test_reset_strings_use_the_device_format(self):
        r = self.rec(cx_raw(cx_limits(primary=cx_win(40, 10080), secondary=cx_win(12, 300, 1789641634))))
        self.assertEqual(r["u"]["sr"], "10:40am")
        self.assertEqual(r["u"]["wr"], "Sep 24 3:24am")

    def test_resets_are_iso_strings_for_the_notifier(self):
        r = self.rec()
        self.assertEqual(r["resets"], {"session": None, "week": "2026-09-24T03:24:10+00:00"})

    def test_swapped_slots_still_classify_by_duration(self):
        r = self.rec(cx_raw(cx_limits(primary=cx_win(40, 10080), secondary=cx_win(12, 300))))
        self.assertEqual((r["u"]["s"], r["u"]["w"]), (12, 40))

    def test_two_windows_without_durations_fall_back_to_slot_order(self):
        r = self.rec(cx_raw(cx_limits(primary={"usedPercent": 5}, secondary={"usedPercent": 60})))
        self.assertEqual((r["u"]["s"], r["u"]["w"]), (5, 60))

    def test_a_lone_window_without_a_duration_is_the_weekly(self):
        r = self.rec(cx_raw(cx_limits(primary={"usedPercent": 33}, secondary=None)))
        self.assertEqual((r["u"]["s"], r["u"]["w"]), (-1, 33))

    def test_a_three_day_window_is_shown_under_week_rather_than_dropped(self):
        self.assertEqual(self.rec(cx_raw(cx_limits(primary=cx_win(8, 4320), secondary=None)))["u"]["w"], 8)

    def test_falls_back_to_rate_limits_when_the_codex_bucket_is_absent(self):
        d = json.loads(json.dumps(CODEX_LIMITS)); del d["result"]["rateLimitsByLimitId"]["codex"]
        self.assertEqual(self.rec(cx_raw(d))["u"]["w"], 19)

    def test_scoped_bucket_is_off_by_default_and_on_when_named(self):
        self.assertEqual((self.rec()["u"]["f"], self.rec()["u"]["fl"]), (-1, ""))
        u = self.rec(scoped="spark")["u"]
        self.assertEqual((u["f"], u["fl"]), (7, "SPARK"))
        self.assertEqual(self.rec(scoped="bengalfox")["u"]["f"], 7)

    def test_every_bucket_is_listed_for_the_terminal(self):
        self.assertEqual([b["id"] for b in self.rec()["buckets"]], ["codex", "codex_bengalfox"])

    def test_blocked_follows_the_backend_verdict(self):
        self.assertFalse(self.rec()["blocked"])
        d = json.loads(json.dumps(CODEX_LIMITS)); d["result"]["ordinaryUsageAllowed"] = False
        self.assertTrue(self.rec(cx_raw(d))["blocked"])
        self.assertTrue(self.rec(cx_raw(cx_limits(rateLimitReachedType="rate_limit_reached")))["blocked"])

    def test_a_reply_with_no_windows_is_stale_not_zero(self):
        d = json.loads(json.dumps(CODEX_LIMITS))
        d["result"]["rateLimitsByLimitId"] = {}; d["result"]["rateLimits"] = {}
        r = self.rec(cx_raw(d))
        self.assertEqual((r["stale"], r["err"], r["u"]["w"]), (True, "no_windows", -1))

    def test_dead_and_failed_reads_are_stale(self):
        dead = self.rec(cx_raw(cx_err(-32603, CX_HTTP % ("401 Unauthorized", "application/json"))))
        self.assertEqual((dead["auth"], dead["stale"]), ("dead", True))


class TestCodexLastGood(TzPinned):
    def test_a_failed_read_keeps_showing_the_last_good_numbers(self):
        good = srv.codex_with_last_good(srv.codex_account_from_rpc("default", "", "/h", cx_raw()), None, 1000.0)
        bad = srv.codex_with_last_good(
            srv.codex_account_from_rpc("default", "", "/h", cx_raw(None, None, "timeout")), good, 1400.0)
        self.assertEqual(bad["u"]["w"], 19)
        self.assertEqual((bad["stale"], bad["err"], bad["age"], bad["label"]), (True, "timeout", 400, "COSMO"))

    def test_a_failed_read_with_no_history_stays_unknown(self):
        bad = srv.codex_with_last_good(
            srv.codex_account_from_rpc("default", "", "/h", cx_raw(None, None, "timeout")), None, 1.0)
        self.assertEqual((bad["u"]["s"], bad["u"]["w"]), (-1, -1))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python -m unittest test_collector.TestCodexMapping test_collector.TestCodexLastGood`
Expected: FAIL with `AttributeError: ... has no attribute 'codex_account_from_rpc'`.

- [ ] **Step 3: Write minimal implementation**

In the Codex section, directly under the section's opening comment block (above `_CODEX_HTTP`), add:

```python
CODEX_MAIN_BUCKET = "codex"
DAY_MINS, WEEK_MINS = 1440, 10080
```

After `codex_status`, add:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run the two classes, then the full suite. Expected: PASS; `Ran 109 tests`, `OK`.

- [ ] **Step 5: Commit**

```bash
rtk git add host/claude_usage_server.py host/test_collector.py
rtk git commit -m "codex: map rate-limit replies into account records, windows classified by duration"
```

---

### Task 3: Discovery, binary resolution, config keys

**Files:**
- Modify: `host/claude_usage_server.py` (`EDITABLE`, `DEFAULTS`, Codex section)
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: `CONFIG`.
- Produces:
  - `_cfg_num(key, default) -> float`
  - `codex_bin() -> str` (`""` when absent)
  - `codex_version() -> str`
  - `codex_homes(only="", default_home=None, root=None, seen=()) -> [(slot, alias, path)]`
  - module constants `CODEX_DEFAULT_HOME`, `CODEX_HOMES_ROOT`, `CODEX_DEAD_HOLD = 1800`
  - config keys `CODEX_BIN`, `CODEX_ACCOUNTS`, `CODEX_EVERY` ("300"), `CODEX_TIMEOUT` ("20"), `CODEX_SCOPED`, `CODEX_MAXED_THRESHOLD` ("100")

- [ ] **Step 1: Write the failing test**

```python
class TestCodexDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dot = os.path.join(self.tmp.name, "dot"); self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(self.dot)
        for n in ("Work", "alt"): os.makedirs(os.path.join(self.root, n))

    def tearDown(self): self.tmp.cleanup()

    def homes(self, only="", seen=()):
        return srv.codex_homes(only, self.dot, self.root, seen)

    def login(self): open(os.path.join(self.dot, "auth.json"), "w").close()

    def test_default_home_counts_only_once_it_holds_a_login(self):
        self.assertNotIn("default", [h[0] for h in self.homes()])
        self.login()
        self.assertEqual(self.homes()[0], ("default", "", self.dot))

    def test_every_directory_under_the_root_is_an_account_logged_in_or_not(self):
        self.assertEqual(sorted(h[0] for h in self.homes()), ["alt", "work"])

    def test_slot_is_lowercase_but_the_alias_keeps_its_case(self):
        self.assertIn(("work", "Work", os.path.join(self.root, "Work")), self.homes())

    def test_filter_selects_and_orders_and_ignores_unknown_names(self):
        self.login()
        self.assertEqual([h[0] for h in self.homes("alt, default,nope")], ["alt", "default"])

    def test_a_default_home_seen_good_survives_a_logout_so_it_can_show_login_expired(self):
        self.assertEqual([h[0] for h in self.homes("default", seen={"default"})], ["default"])

    def test_a_missing_root_is_not_an_error(self):
        self.assertEqual(srv.codex_homes("", self.dot, os.path.join(self.tmp.name, "nope")), [])

    def test_a_directory_named_default_cannot_shadow_the_real_default(self):
        os.makedirs(os.path.join(self.root, "default")); self.login()
        self.assertEqual([h[0] for h in self.homes()].count("default"), 1)


class TestCodexConfig(unittest.TestCase):
    def test_new_keys_have_working_defaults_and_are_editable(self):
        for k, v in (("CODEX_BIN", ""), ("CODEX_ACCOUNTS", ""), ("CODEX_EVERY", "300"),
                     ("CODEX_TIMEOUT", "20"), ("CODEX_SCOPED", ""), ("CODEX_MAXED_THRESHOLD", "100")):
            self.assertEqual(srv.DEFAULTS[k], v); self.assertIn(k, srv.EDITABLE)

    def test_cfg_num_survives_garbage(self):
        old = srv.CONFIG.get("CODEX_EVERY"); srv.CONFIG["CODEX_EVERY"] = "banana"
        try: self.assertEqual(srv._cfg_num("CODEX_EVERY", 300), 300.0)
        finally: srv.CONFIG["CODEX_EVERY"] = old

    def test_an_explicit_binary_that_does_not_exist_resolves_to_nothing(self):
        old = srv.CONFIG.get("CODEX_BIN"); srv.CONFIG["CODEX_BIN"] = "/definitely/not/here/codex"
        try: self.assertEqual(srv.codex_bin(), "")
        finally: srv.CONFIG["CODEX_BIN"] = old
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python -m unittest test_collector.TestCodexDiscovery test_collector.TestCodexConfig`
Expected: FAIL (`codex_homes` missing, `KeyError: 'CODEX_BIN'`).

- [ ] **Step 3: Write minimal implementation**

In `EDITABLE`, after the line `"CSWAP_BIN", "CSWAP_ACCOUNTS", "UPDATE_EVERY",` add:

```python
            # --- accounts: codex is the optional second source, one CODEX_HOME per account ---
            "CODEX_BIN", "CODEX_ACCOUNTS", "CODEX_EVERY", "CODEX_TIMEOUT", "CODEX_SCOPED",
            "CODEX_MAXED_THRESHOLD",
```

In `DEFAULTS`, after the line `"CSWAP_BIN": "", "CSWAP_ACCOUNTS": "", "UPDATE_EVERY": "21600",` add:

```python
            # Every Codex read is one live backend request per account, against a private
            # endpoint, so this is deliberately slow. 300 is also the enforced floor.
            "CODEX_BIN": "", "CODEX_ACCOUNTS": "", "CODEX_EVERY": "300", "CODEX_TIMEOUT": "20",
            "CODEX_SCOPED": "", "CODEX_MAXED_THRESHOLD": "100",
```

In the Codex section, extend the constants block to:

```python
CODEX_MAIN_BUCKET = "codex"
DAY_MINS, WEEK_MINS = 1440, 10080
CODEX_DEAD_HOLD = 1800                  # a dead login is re-read this often, not every poll
CODEX_DEFAULT_HOME = os.path.expanduser("~/.codex")
CODEX_HOMES_ROOT = os.path.expanduser("~/.claudetv/codex")
```

and add directly below it:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Expected: PASS; full suite `Ran 119 tests`, `OK`.

- [ ] **Step 5: Commit**

```bash
rtk git add host/claude_usage_server.py host/test_collector.py
rtk git commit -m "codex: account discovery by CODEX_HOME, binary resolution, config keys"
```

---

### Task 4: RPC driver, concurrent poll, wall-clock budget

**Files:**
- Modify: `host/claude_usage_server.py` (import line; Codex section)
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (deliberately independent of the mapper).
- Produces:
  - `codex_rpc_read(exe, home, deadline_s=20.0) -> {"account": reply|None, "limits": reply|None, "driver_err": ""|"timeout"|"unavailable"}`. `exe` is a path string or an argv list. Never raises. Always reaps.
  - `codex_poll(exe, homes, deadline_s, reader=None) -> [raw, ...]` in `homes` order.

- [ ] **Step 1: Write the failing test**

```python
FAKE_CODEX = r'''
import json, os, sys, time
home = os.environ.get("CODEX_HOME", "")
def rd(n, d=""):
    try: return open(os.path.join(home, n), encoding="utf-8").read().strip()
    except Exception: return d
mode = rd("mode", "ok")
json.dump(sys.argv[1:], open(os.path.join(home, "argv.json"), "w"))
def out(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
if mode == "exit": sys.exit(3)
for line in sys.stdin:
    try: m = json.loads(line)
    except ValueError: continue
    meth, i = m.get("method"), m.get("id")
    if meth == "initialize":
        if mode == "noinit": time.sleep(600)
        sys.stdout.write("WARN not json at all\n"); sys.stdout.flush()
        out({"method": "remoteControl/status/changed", "params": {}})
        out({"id": i, "result": {"userAgent": "fake"}})
    elif meth == "account/read":
        out({"id": i, "result": {"account": None if mode == "loggedout" else
             {"type": "chatgpt", "email": "cosmo@example.com", "planType": "prolite"},
             "requiresOpenaiAuth": True}})
    elif meth == "account/rateLimits/read":
        json.dump(m.get("params"), open(os.path.join(home, "params.json"), "w"))
        if mode == "hang": time.sleep(600)
        elif mode == "loggedout":
            out({"id": i, "error": {"code": -32600,
                 "message": "codex account authentication required to read rate limits"}})
        else: out({"id": i, "result": json.loads(rd("limits.json", "{}"))})
'''


class TestCodexDriver(unittest.TestCase):
    """Drives a fake `codex` that speaks the same stdio protocol. The real binary is covered by
    TestCodexLiveContract (opt-in) and by the acceptance run in Task 8."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = os.path.join(self.tmp.name, "fake_codex.py")
        with open(fake, "w", encoding="utf-8") as f: f.write(FAKE_CODEX)
        self.exe = [sys.executable, fake]
        self.made = []; made = self.made
        self._popen = subprocess.Popen

        class Spy(subprocess.Popen):
            def __init__(self, *a, **k):
                super().__init__(*a, **k); made.append(self)
        subprocess.Popen = Spy

    def tearDown(self):
        subprocess.Popen = self._popen
        for p in self.made:
            try: p.kill()
            except Exception: pass
        self.tmp.cleanup()

    def home(self, mode, name):
        p = os.path.join(self.tmp.name, name); os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "mode"), "w") as f: f.write(mode)
        with open(os.path.join(p, "limits.json"), "w") as f: json.dump(CODEX_LIMITS["result"], f)
        return p

    def test_happy_path_reads_through_log_chatter_and_notifications(self):
        o = srv.codex_rpc_read(self.exe, self.home("ok", "a"), 10)
        self.assertEqual(o["driver_err"], "")
        self.assertEqual(o["limits"]["result"]["accountId"], "acc-1")
        self.assertEqual(o["account"]["result"]["account"]["planType"], "prolite")

    def test_launches_lean_and_polls_politely(self):
        h = self.home("ok", "a"); srv.codex_rpc_read(self.exe, h, 10)
        def load(name):
            with open(os.path.join(h, name)) as f: return json.load(f)
        self.assertEqual(load("argv.json"), ["app-server", "--disable", "plugins"])
        self.assertEqual(load("params.json"), {"excludeResetCreditDetails": True})

    def test_the_child_is_always_reaped(self):
        for mode in ("ok", "loggedout", "exit", "hang"):
            srv.codex_rpc_read(self.exe, self.home(mode, "r_" + mode), 1.0)
        self.assertEqual([p.poll() is not None for p in self.made], [True] * 4)

    def test_a_logged_out_home_reaches_the_status_table_as_dead(self):
        o = srv.codex_rpc_read(self.exe, self.home("loggedout", "a"), 10)
        self.assertEqual(srv.codex_status(o["account"], o["limits"], o["driver_err"]), ("dead", "login_required"))

    def test_a_server_that_exits_early_is_unavailable_not_an_exception(self):
        self.assertEqual(srv.codex_rpc_read(self.exe, self.home("exit", "a"), 10)["driver_err"], "unavailable")

    def test_a_missing_binary_is_unavailable_not_an_exception(self):
        self.assertEqual(srv.codex_rpc_read(["definitely-not-a-binary-xyz"], self.home("ok", "a"), 5)["driver_err"],
                         "unavailable")

    def test_no_handshake_times_out_inside_the_deadline(self):
        t0 = time.monotonic(); o = srv.codex_rpc_read(self.exe, self.home("noinit", "a"), 1.5)
        self.assertEqual(o["driver_err"], "timeout"); self.assertLess(time.monotonic() - t0, 6.0)

    def test_largest_plausible_input_eight_hung_accounts_within_a_wall_clock_budget(self):
        """MAXACC is 8 and Codex never abandons a hung backend. The budget is measured time:
        one deadline plus the 2s graceful-exit wait, NOT eight of them."""
        homes = [("h%d" % i, "h%d" % i, self.home("hang", "hang%d" % i)) for i in range(8)]
        t0 = time.monotonic(); res = srv.codex_poll(self.exe, homes, 2.0); wall = time.monotonic() - t0
        self.assertEqual([r["driver_err"] for r in res], ["timeout"] * 8)
        self.assertLess(wall, 8.0, "8 hung accounts took %.1fs: reads are not concurrent" % wall)
        self.assertEqual(len(self.made), 8)
        self.assertTrue(all(p.poll() is not None for p in self.made), "a hung child was left running")

    def test_one_hung_home_does_not_poison_the_others(self):
        homes = [("a", "a", self.home("ok", "m_ok")), ("b", "b", self.home("hang", "m_hang")),
                 ("c", "c", self.home("loggedout", "m_lo"))]
        res = srv.codex_poll(self.exe, homes, 2.0)
        self.assertEqual([r["driver_err"] for r in res], ["", "timeout", ""])
        self.assertEqual(res[2]["limits"]["error"]["code"], -32600)

    def test_a_reader_that_raises_is_contained(self):
        def boom(exe, home, deadline): raise RuntimeError("nope")
        res = srv.codex_poll("x", [("a", "a", "/h")], 1.0, reader=boom)
        self.assertEqual(res, [{"account": None, "limits": None, "driver_err": "timeout"}])
```

Add `subprocess, sys, time` to the test module's import line (it currently imports `json, os, tempfile, unittest`).

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python -m unittest test_collector.TestCodexDriver`
Expected: FAIL with `AttributeError: ... has no attribute 'codex_rpc_read'`.

- [ ] **Step 3: Write minimal implementation**

On the server's import line, add `signal` (keep the list alphabetical: `... shutil, signal, subprocess, ...`).

In the Codex section, after `codex_with_last_good`, add:

```python
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
```

This exact code was validated before the plan was written, against the fake and against the real codex-cli 0.154.0, on Windows and on the Ubuntu production host: 8 hung accounts at a 2 s deadline took 4.02 s on both, with zero children left.

- [ ] **Step 4: Run test to verify it passes**

Run `TestCodexDriver` (about 12 s because of the deliberate hangs), then the full suite.
Expected: PASS; `Ran 129 tests`, `OK`. Read the wall-clock: if the budget test takes more than 8 s, the reads are serial. Fix that, do not raise the budget.

- [ ] **Step 5: Commit**

```bash
rtk git add host/claude_usage_server.py host/test_collector.py
rtk git commit -m "codex: app-server RPC driver with an owned deadline, concurrent per-account reads"
```

---

### Task 5: Source integration (isolation, scheduling, dead hold)

**Files:**
- Modify: `host/claude_usage_server.py` (globals near `_accounts`; Codex section; `poller`; `device_json`; `full_state`; `do_POST`; `__main__`)
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: `codex_bin`, `codex_homes`, `codex_poll`, `codex_account_from_rpc`, `codex_with_last_good`, `_cfg_num`, `CODEX_DEAD_HOLD`; existing `_lock`, `_auth_transitions`, `notifiable`, `notify_check`, `notify_key`, `_load_notify_state`, `_save_notify_state`. `codex_fleet_check` arrives in Task 6, so this task calls it through a guard (see Step 3).
- Produces:
  - `_src_accts = {"claude": [], "codex": []}` and `_publish(source, accts)`
  - `_codex` state dict with keys `accts, ts, next, fails, force, last, good, hold, ident, pending, fleet`
  - `codex_refresh(now, force=False, reader=None) -> [record]`
  - `codex_next_delay(accts, fails) -> float seconds`
  - `codex_io_loop()` (thread body) and `codex_apply_pending() -> bool` (poller thread)

- [ ] **Step 1: Write the failing test**

```python
class CodexIsolated(TzPinned):
    """Notifier files in a temp dir, no channels configured, Codex source state reset."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self._paths = (srv.NOTIFY_STATE_PATH, srv.RESET_LOG_PATH)
        srv.NOTIFY_STATE_PATH = os.path.join(self.tmp.name, "notify_state.json")
        srv.RESET_LOG_PATH = os.path.join(self.tmp.name, "resets.log")
        srv._notify_state = None; srv._reset_log = None
        self._cfg = {k: srv.CONFIG.get(k) for k in ("NOTIFY_DISCORD_WEBHOOK", "NOTIFY_SLACK_WEBHOOK",
                     "NOTIFY_EMAIL", "CODEX_ACCOUNTS", "CODEX_EVERY", "CODEX_SCOPED")}
        for k in ("NOTIFY_DISCORD_WEBHOOK", "NOTIFY_SLACK_WEBHOOK", "NOTIFY_EMAIL", "CODEX_ACCOUNTS", "CODEX_SCOPED"):
            srv.CONFIG[k] = ""
        srv.CONFIG["CODEX_EVERY"] = "300"
        self._fn = {k: getattr(srv, k) for k in ("codex_bin", "codex_homes")}
        srv.codex_bin = lambda: "codex"
        self.homes = [("default", "", "/h/default"), ("work", "work", "/h/work")]
        srv.codex_homes = lambda only="", default_home=None, root=None, seen=(): list(self.homes)
        self._state = (dict(srv._codex), {k: list(v) for k, v in srv._src_accts.items()}, list(srv._accounts),
                       dict(srv._alerted))
        srv._codex.update({"accts": [], "ts": 0, "next": 0.0, "fails": 0, "force": False, "last": 0.0,
                           "good": {}, "hold": {}, "ident": {}, "pending": None, "fleet": {}})
        srv._src_accts.update({"claude": [], "codex": []}); srv._accounts = []; srv._alerted.clear()
        self.calls = []

    def tearDown(self):
        srv._codex.clear(); srv._codex.update(self._state[0])
        srv._src_accts.update(self._state[1]); srv._accounts = self._state[2]
        srv._alerted.clear(); srv._alerted.update(self._state[3])
        for k, v in self._fn.items(): setattr(srv, k, v)
        srv.NOTIFY_STATE_PATH, srv.RESET_LOG_PATH = self._paths
        srv._notify_state = None; srv._reset_log = None
        srv.CONFIG.update(self._cfg); self.tmp.cleanup()
        super().tearDown()

    def reader(self, by_home):
        """A stub for codex_rpc_read: `by_home` maps a home path to the raw read it returns."""
        def read(exe, home, deadline):
            self.calls.append(home); return by_home[home]
        return read


class TestCodexSource(CodexIsolated):
    DEAD = cx_raw(cx_err(-32603, CX_HTTP % ("401 Unauthorized", "application/json")))

    def test_publish_orders_claude_first_and_a_source_cannot_blank_the_other(self):
        srv._publish("codex", [{"label": "X"}]); srv._publish("claude", [{"label": "A"}])
        self.assertEqual([a["label"] for a in srv._accounts], ["A", "X"])
        srv._publish("codex", [])
        self.assertEqual([a["label"] for a in srv._accounts], ["A"])
        srv._publish("codex", [{"label": "X"}]); srv._publish("claude", [])
        self.assertEqual([a["label"] for a in srv._accounts], ["X"])

    def test_refresh_maps_every_home_in_order(self):
        accts = srv.codex_refresh(1000.0, reader=self.reader({"/h/default": cx_raw(), "/h/work": cx_raw()}))
        self.assertEqual([a["key"] for a in accts], ["codex:default", "codex:work"])
        self.assertEqual([a["label"] for a in accts], ["COSMO", "WORK"])

    def test_no_binary_or_no_homes_is_an_empty_source_not_an_error(self):
        srv.codex_bin = lambda: ""
        self.assertEqual(srv.codex_refresh(1.0, reader=self.reader({})), [])
        srv.codex_bin = lambda: "codex"; self.homes = []
        self.assertEqual(srv.codex_refresh(1.0, reader=self.reader({})), [])

    def test_a_dead_login_is_held_for_thirty_minutes_while_the_others_keep_their_cadence(self):
        rd = self.reader({"/h/default": self.DEAD, "/h/work": cx_raw()})
        srv.codex_refresh(1000.0, reader=rd)
        self.calls.clear(); accts = srv.codex_refresh(1300.0, reader=rd)
        self.assertEqual(self.calls, ["/h/work"])
        self.assertEqual([a["auth"] for a in accts], ["dead", "ok"])      # the held record is kept
        self.calls.clear(); srv.codex_refresh(1000.0 + srv.CODEX_DEAD_HOLD, reader=rd)
        self.assertEqual(sorted(self.calls), ["/h/default", "/h/work"])

    def test_a_forced_read_overrides_the_hold_so_a_relogin_shows_on_demand(self):
        rd = self.reader({"/h/default": self.DEAD, "/h/work": cx_raw()})
        srv.codex_refresh(1000.0, reader=rd); self.calls.clear()
        srv.codex_refresh(1010.0, force=True, reader=rd)
        self.assertEqual(sorted(self.calls), ["/h/default", "/h/work"])

    def test_a_failed_read_serves_last_good_and_recovers(self):
        ok = {"/h/default": cx_raw(), "/h/work": cx_raw()}
        srv.codex_refresh(1000.0, reader=self.reader(ok))
        bad = dict(ok); bad["/h/work"] = cx_raw(None, None, "timeout")
        accts = srv.codex_refresh(1300.0, reader=self.reader(bad))
        self.assertEqual((accts[1]["u"]["w"], accts[1]["stale"], accts[1]["age"]), (19, True, 300))
        self.assertFalse(srv.codex_refresh(1600.0, reader=self.reader(ok))[1]["stale"])

    def test_delay_never_drops_below_the_floor_and_only_failures_slow_it(self):
        ok = srv.codex_account_from_rpc("a", "a", "/h", cx_raw())
        bad = srv.codex_account_from_rpc("b", "b", "/h", cx_raw(None, None, "timeout"))
        rl = srv.codex_account_from_rpc("c", "c", "/h", cx_raw(cx_err(-32603, CX_HTTP % ("429 Too Many Requests", ""))))
        dead = srv.codex_account_from_rpc("d", "d", "/h", self.DEAD)
        srv.CONFIG["CODEX_EVERY"] = "60"                      # below the floor on purpose
        self.assertEqual(srv.codex_next_delay([ok], 0), 300)
        self.assertEqual(srv.codex_next_delay([ok, bad], 1), 300)     # one failure: healthy ones keep cadence
        self.assertEqual(srv.codex_next_delay([bad], 1), 300)
        self.assertEqual(srv.codex_next_delay([bad], 3), 1200)        # source-wide outage doubles
        self.assertEqual(srv.codex_next_delay([bad], 9), 1800)        # capped
        self.assertEqual(srv.codex_next_delay([ok, rl], 1), 900)      # rate limited starts at 15 min
        self.assertEqual(srv.codex_next_delay([ok, dead], 0), 300)    # a dead login does not slow the rest
        self.assertEqual(srv.codex_next_delay([], 0), 300)

    # Both windows present: until the alerts task relaxes notifiable() for Codex, a weekly-only
    # reading is not fed to the notifier, and these two tests would pass without proving anything.
    BOTH = cx_limits(primary=cx_win(90, 10080), secondary=cx_win(50, 300, 1789641634))

    def test_apply_publishes_and_runs_reset_detection_on_the_poller_thread(self):
        srv._codex["pending"] = srv.codex_refresh(1000.0, reader=self.reader(
            {"/h/default": cx_raw(self.BOTH), "/h/work": cx_raw(self.BOTH)}))
        self.assertTrue(srv.codex_apply_pending())
        self.assertEqual([a["key"] for a in srv._accounts], ["codex:default", "codex:work"])
        self.assertIn("codex:default", srv._load_notify_state())
        self.assertIsNone(srv._codex["pending"])
        self.assertFalse(srv.codex_apply_pending())               # nothing pending: a no-op

    def test_a_home_relogged_into_another_account_baselines_instead_of_firing_a_reset(self):
        rd1 = self.reader({"/h/default": cx_raw(self.BOTH), "/h/work": cx_raw(self.BOTH)})
        srv._codex["pending"] = srv.codex_refresh(1000.0, reader=rd1); srv.codex_apply_pending()
        other = cx_limits(primary=cx_win(2, 10080), secondary=cx_win(1, 300, 1789641634))
        other["result"]["accountId"] = "acc-2"
        rd2 = self.reader({"/h/default": cx_raw(other), "/h/work": cx_raw(self.BOTH)})
        srv._codex["pending"] = srv.codex_refresh(1300.0, reader=rd2); srv.codex_apply_pending()
        # 90 -> 2 and 50 -> 1 on the same key would otherwise log two phantom "gift" resets
        self.assertEqual(srv._load_reset_log(), [])
        self.assertEqual(srv._load_notify_state()["codex:default"]["week"]["w"], 2)   # re-baselined

    def test_a_codex_only_host_serves_the_device_without_a_claude_error(self):
        old = (srv._usage_ts, srv._usage_err); srv._usage_ts, srv._usage_err = 0, "claude-swap is not installed on this host"
        try:
            srv._codex["pending"] = srv.codex_refresh(time.time(), reader=self.reader({"/h/default": cx_raw(), "/h/work": cx_raw()}))
            srv.codex_apply_pending()
            d = srv.device_json()
            self.assertEqual((d["n"], d["w"], d["err"]), (2, 19, ""))
            self.assertGreaterEqual(d["age"], 0)
        finally: srv._usage_ts, srv._usage_err = old

    def test_a_codex_failure_leaves_the_claude_wire_output_byte_identical(self):
        claude = srv.cswap_accounts_from_json(doc())
        srv._publish("claude", claude)
        before = json.dumps(srv.usage_wire(list(srv._src_accts["claude"]), {}), sort_keys=True)
        srv.codex_bin = lambda: "codex"
        def boom(exe, home, deadline): raise RuntimeError("codex exploded")
        srv._codex["pending"] = srv.codex_refresh(1000.0, reader=boom); srv.codex_apply_pending()
        after = json.dumps(srv.usage_wire(list(srv._src_accts["claude"]), {}), sort_keys=True)
        self.assertEqual(before, after)
        self.assertEqual([a["label"] for a in srv._accounts[:2]], ["WORK", "PERSONAL"])

    def test_state_reports_both_sources(self):
        s = srv.full_state()["accounts"]["sources"]
        self.assertEqual(sorted(s), ["claude", "codex"])
        for k in ("bin", "ver", "n", "age", "next_in", "filter"): self.assertIn(k, s["codex"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python -m unittest test_collector.TestCodexSource`
Expected: FAIL with `AttributeError: ... has no attribute '_codex'`.

- [ ] **Step 3: Write minimal implementation**

(a) Next to the other globals (after the line that defines `_force_poll`), add:

```python
_src_accts = {"claude": [], "codex": []}   # each source's last-good list; _accounts is their join

def _publish(source, accts):
    """Swap in ONE source's records. `_accounts` is always Claude first, then Codex, so a source
    that fails keeps its own last-good list and can never blank the other."""
    global _accounts
    with _lock:
        _src_accts[source] = list(accts)
        _accounts = _src_accts["claude"] + _src_accts["codex"]
```

(b) In the Codex section, after `codex_poll`, add:

```python
_codex = {"accts": [], "ts": 0, "next": 0.0, "fails": 0, "force": False, "last": 0.0,
          "good": {}, "hold": {}, "ident": {}, "pending": None, "fleet": {}}

def codex_refresh(now, force=False, reader=None):
    """One I/O pass: discover, read, map, merge last-good. Pure I/O and mapping: it touches no
    notifier state, which stays on the poller thread (see codex_apply_pending). Never raises
    for a per-account failure; a missing binary or no homes is simply an empty source."""
    exe = codex_bin()
    if not exe: return []
    homes = codex_homes(CONFIG.get("CODEX_ACCOUNTS", ""), seen=set(_codex["good"]))
    if not homes: return []
    prev = {a["slot"]: a for a in _codex["accts"]}
    # A dead login is re-read only every CODEX_DEAD_HOLD: every read of a dead home makes a fresh
    # Codex process retry a refresh that cannot succeed, and polling cannot fix a login.
    due = [h for h in homes if force or h[0] not in prev or now >= _codex["hold"].get(h[0], 0)]
    raws = dict(zip([h[0] for h in due], codex_poll(exe, due, _cfg_num("CODEX_TIMEOUT", 20), reader)))
    out = []
    for slot, alias, path in homes:
        if slot not in raws: out.append(prev[slot]); continue
        rec = codex_with_last_good(codex_account_from_rpc(slot, alias, path, raws[slot],
                                   CONFIG.get("CODEX_SCOPED", "")), _codex["good"].get(slot), now)
        if not rec["stale"]: _codex["good"][slot] = rec
        if rec["auth"] == "dead": _codex["hold"][slot] = now + CODEX_DEAD_HOLD
        else: _codex["hold"].pop(slot, None)
        out.append(rec)
    _codex["accts"] = out
    return out

def codex_next_delay(accts, fails):
    """Seconds until the next pass. A failure NEVER makes the source faster than CODEX_EVERY
    (floor 300): one account failing must not drag the healthy ones below the floor. Failures
    only slow it down, and only when they are source-wide or the backend said 429."""
    base = max(300.0, _cfg_num("CODEX_EVERY", 300))
    live = [a for a in accts if a["auth"] != "dead"]
    if any(a["err"] == "rate_limited" for a in live): return min(1800.0, 900.0 * (2 ** max(0, fails - 1)))
    if live and all(a["stale"] for a in live): return min(1800.0, base * (2 ** max(0, fails - 1)))
    return base

def codex_io_loop():
    """Codex I/O on its own thread, so a hung backend can never delay the Claude poll. It only
    reads and maps; every stateful pass happens on the poller thread via codex_apply_pending,
    which keeps notifier state single-threaded."""
    while True:
        now = time.time()
        force, _codex["force"] = _codex["force"], False
        force = force and now - _codex["last"] >= 60          # the button cannot hammer the backend
        if now >= _codex["next"] or force:
            try:
                accts = codex_refresh(now, force)
                live = [a for a in accts if a["auth"] != "dead"]
                troubled = any(a["err"] == "rate_limited" for a in live) or (bool(live) and all(a["stale"] for a in live))
                _codex["fails"] = _codex["fails"] + 1 if troubled else 0
                with _lock: _codex["pending"] = accts
                _codex["next"] = now + codex_next_delay(accts, _codex["fails"])
            except Exception as e:
                print("[codex] poll error: %s" % str(e)[:90]); _codex["next"] = now + 300
            _codex["last"] = now
        time.sleep(2)

def codex_apply_pending():
    """Poller thread: publish a finished Codex pass and run the stateful passes on it."""
    with _lock: accts, _codex["pending"] = _codex["pending"], None
    if accts is None: return False
    for rec in accts:
        was = _codex["ident"].get(rec["slot"])
        if rec["ident"] and was and was != rec["ident"]:       # home re-logged into another account:
            _load_notify_state().pop(rec["key"], None); _save_notify_state()   # baseline, no phantom reset
        if rec["ident"]: _codex["ident"][rec["slot"]] = rec["ident"]
    _publish("codex", accts)
    with _lock: _codex["ts"] = int(time.time())
    _auth_transitions(accts)
    for rec in accts:
        if notifiable(rec):
            notify_check(rec["u"], rec["resets"], acct=notify_key(rec), label=rec["label"])
    check = globals().get("codex_fleet_check")                # arrives in the alerts task
    if check:
        f = check(accts)
        with _lock: _codex["fleet"] = f
    return True
```

(c) In `poller()`: replace the two-line block

```python
                with _lock:
                    _accounts = accts; _usage_ts = int(now); _usage_err = ""
```

with

```python
                _publish("claude", accts)
                with _lock: _usage_ts = int(now); _usage_err = ""
```

and, as the first statement inside `while True:` (before `now = time.time()`), add:

```python
        try: codex_apply_pending()
        except Exception as e: print("[codex] apply error: %s" % str(e)[:90])
```

(d) In `device_json()`, replace the first line (`with _lock: accts, ts, err, wx = ...`) with:

```python
    with _lock:
        accts, ts, err, wx = list(_accounts), _usage_ts, _usage_err, _wx
        # a Codex-only host has no Claude source to report on: its clock and errors are Codex's
        if not _src_accts["claude"] and _src_accts["codex"]: ts, err = _codex["ts"], ""
```

(e) In `full_state()`, inside the `"accounts"` dict, add a `"sources"` entry and extend each list item. Replace the `"accounts": {...}` entry with:

```python
            "accounts": {"ready": bool(accts), "source_err": _source_err, "cswap": cswap_bin(),
                         "cswap_ver": cswap_version(), "filter": CONFIG.get("CSWAP_ACCOUNTS", ""),
                         "sources": {
                             "claude": {"bin": cswap_bin(), "ver": cswap_version(), "err": _source_err,
                                        "n": len(_src_accts["claude"]),
                                        "filter": CONFIG.get("CSWAP_ACCOUNTS", "")},
                             "codex": {"bin": codex_bin(), "ver": codex_version(),
                                       "n": len(_src_accts["codex"]),
                                       "age": (int(time.time()) - _codex["ts"]) if _codex["ts"] else -1,
                                       "next_in": max(0, int(_codex["next"] - time.time())),
                                       "filter": CONFIG.get("CODEX_ACCOUNTS", "")}},
                         "list": [{"key": a["key"], "label": a["label"], "email": a["email"],
                                   "active": a["active"], "auth": a["auth"],
                                   "age": (int(time.time() - a["good_at"]) if a.get("good_at") else a["age"]),
                                   "err": a["err"], "stale": a.get("stale", False),
                                   "disabled": a.get("disabled", False),
                                   "provider": a.get("provider", "claude"), "plan": a.get("plan", ""),
                                   "blocked": a.get("blocked", False), "home": a.get("home", ""),
                                   "buckets": a.get("buckets", []), **a["u"]} for a in accts]},
```

Also add `"codex_fleet": dict(_codex["fleet"]),` right after the existing `"fleet": ...` entry.

(f) In `do_POST`, in the `action == "poll"` branch, after the `_force_poll` line add:

```python
            _codex["force"] = True
```

(g) In `__main__`, after the line that starts the `poller` thread, add:

```python
    threading.Thread(target=codex_io_loop, daemon=True).start()
```

- [ ] **Step 4: Run test to verify it passes**

Run `TestCodexSource`, then the full suite. Expected: PASS; `Ran 141 tests`, `OK`.

- [ ] **Step 5: Commit**

```bash
rtk git add host/claude_usage_server.py host/test_collector.py
rtk git commit -m "codex: second account source on its own thread, isolated from the claude poll"
```

---

### Task 6: Alerts (reset detection, LOGIN EXPIRED, Codex fleet)

This is the "relogin" behaviour: a dead Codex login alerts on every enabled channel and takes over that account's card on the device, exactly as a dead Claude login does today. The device part needs no code: `auth: "dead"` in the account's `acc[]` entry already triggers the firmware's `LOGIN EXPIRED / re-auth on host` screen on v5.1.

**Files:**
- Modify: `host/claude_usage_server.py` (`notifiable`, `_AUTH_TITLES`, `_auth_transitions`, `_reset_message`, `notify_check`, `_FLEET_TITLES`, Codex section, `codex_apply_pending`)
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: records from Task 2; `_cfg_num`; existing `_fleet_alert`, `_auth_alert`.
- Produces:
  - `notifiable(rec)`: Codex needs any one window, Claude still needs both
  - `_reset_message(kind, u, cls="expected", maxed=False, provider="claude")`
  - `notify_check(u, resets, acct="", label="", provider="claude")`
  - `_auth_transitions(accts)`: provider-aware wording
  - `codex_fleet_state(accounts, threshold) -> dict`, `codex_fleet_check(accounts) -> dict`
  - `_AUTH_TITLES["codex_dead"]`, `_AUTH_TITLES["codex_recovered"]`, `_FLEET_TITLES["codex_exhausted"]`, `_FLEET_TITLES["codex_recovered"]`

- [ ] **Step 1: Write the failing test**

```python
class TestCodexAlerts(CodexIsolated):
    def setUp(self):
        super().setUp()
        self.auth = []; self.fleet = []
        self._al = (srv._auth_alert, srv._fleet_alert)
        srv._auth_alert = lambda ev, body: self.auth.append((ev, body))
        srv._fleet_alert = lambda ev, body: self.fleet.append((ev, body))
        self._fl = dict(srv._codex_fleet_last); srv._codex_fleet_last.clear()

    def tearDown(self):
        srv._auth_alert, srv._fleet_alert = self._al
        srv._codex_fleet_last.clear(); srv._codex_fleet_last.update(self._fl)
        super().tearDown()

    def acct(self, slot, s=-1, w=-1, **over):
        r = srv.codex_account_from_rpc(slot, slot, "/h/" + slot, cx_raw(cx_limits(
            primary=cx_win(w, 10080) if w >= 0 else None, secondary=cx_win(s, 300) if s >= 0 else None)))
        r.update(over); return r

    # ---- reset detection
    def test_a_weekly_only_account_is_notifiable_but_a_claude_account_still_needs_both(self):
        self.assertTrue(srv.notifiable(self.acct("a", w=19)))
        claude = {"provider": "claude", "stale": False, "u": {"s": -1, "w": 19}}
        self.assertFalse(srv.notifiable(claude))
        self.assertFalse(srv.notifiable({"stale": False, "u": {"s": -1, "w": 19}}))   # no provider = claude

    def test_a_stale_codex_account_is_never_fed_to_the_notifier(self):
        self.assertFalse(srv.notifiable(self.acct("a", w=19, stale=True)))

    def test_a_weekly_only_account_gets_a_week_reset_and_no_phantom_session_reset(self):
        srv.notify_check({"s": -1, "w": 80, "f": -1, "fl": ""}, {"session": None, "week": "2026-09-24T03:00:00+00:00"},
                         acct="codex:a", label="A", provider="codex")
        srv.notify_check({"s": -1, "w": 2, "f": -1, "fl": ""}, {"session": None, "week": "2026-10-01T03:00:00+00:00"},
                         acct="codex:a", label="A", provider="codex")
        self.assertEqual([e["window"] for e in srv._load_reset_log()], ["week"])

    def test_a_short_window_that_disappears_is_not_a_reset(self):
        srv.notify_check({"s": 60, "w": 30, "f": -1, "fl": ""}, {"session": "2026-09-17T10:00:00+00:00",
                         "week": "2026-09-24T03:00:00+00:00"}, acct="codex:a", provider="codex")
        srv.notify_check({"s": -1, "w": 31, "f": -1, "fl": ""}, {"session": None,
                         "week": "2026-09-24T03:00:00+00:00"}, acct="codex:a", provider="codex")
        self.assertEqual(srv._load_reset_log(), [])

    def test_reset_wording_names_the_provider_and_never_prints_a_negative_percent(self):
        title, body = srv._reset_message("week", {"s": -1, "w": 2, "f": -1, "wr": "Oct 1 3am"}, provider="codex")
        self.assertIn("Codex", title); self.assertNotIn("-1", body); self.assertIn("W 2%", body)
        title, _ = srv._reset_message("week", {"s": 5, "w": 2, "f": -1})
        self.assertIn("Claude", title)
        gift, _ = srv._reset_message("week", {"s": -1, "w": 2, "f": -1}, cls="gift", provider="codex")
        self.assertIn("OpenAI", gift)

    # ---- relogin: alert once per outage, recover once, name the remedy
    def test_a_dead_codex_login_alerts_once_with_the_exact_remedy(self):
        dead = self.acct("work", w=19, auth="dead")
        srv._auth_transitions([dead]); srv._auth_transitions([dead])
        self.assertEqual([e for e, _ in self.auth], ["codex_dead"])
        body = self.auth[0][1]
        self.assertIn("WORK", body); self.assertIn("CODEX_HOME=/h/work codex login --device-auth", body)
        self.assertIn("LOGIN EXPIRED", body)

    def test_recovery_alerts_once_after_the_relogin(self):
        srv._auth_transitions([self.acct("work", w=19, auth="dead")])
        srv._auth_transitions([self.acct("work", w=19)]); srv._auth_transitions([self.acct("work", w=19)])
        self.assertEqual([e for e, _ in self.auth], ["codex_dead", "codex_recovered"])

    def test_a_dead_claude_login_keeps_its_original_alert(self):
        rec = {"key": "1:a@e.com", "label": "WORK", "email": "a@e.com", "auth": "dead"}
        srv._auth_transitions([rec])
        self.assertEqual(self.auth[0][0], "dead"); self.assertIn("cswap", self.auth[0][1])

    def test_titles_exist_for_every_event_the_code_can_send(self):
        for k in ("codex_dead", "codex_recovered"): self.assertIn("Codex", srv._AUTH_TITLES[k])
        for k in ("codex_exhausted", "codex_recovered"): self.assertIn("Codex", srv._FLEET_TITLES[k])

    def test_dead_reaches_the_device_as_a_per_account_takeover(self):
        w = srv.usage_wire([self.acct("a", w=19), self.acct("b", w=5, auth="dead")], {})
        self.assertEqual([a["auth"] for a in w["acc"]], ["ok", "dead"])
        self.assertEqual(w["auth"], "ok")                       # one dead account does not kill the screen

    # ---- fleet
    def test_codex_is_out_when_the_backend_says_blocked_or_a_window_hits_the_threshold(self):
        st = srv.codex_fleet_state([self.acct("a", w=100), self.acct("b", w=40, blocked=True)], 100)
        self.assertTrue(st["exhausted"]); self.assertEqual(st["headroom"], [])

    def test_one_account_with_room_means_not_blocked(self):
        st = srv.codex_fleet_state([self.acct("a", w=100), self.acct("b", s=10, w=40)], 100)
        self.assertFalse(st["exhausted"]); self.assertEqual(st["headroom"], ["B"])

    def test_an_unreadable_account_cannot_prove_exhaustion_and_dead_ones_do_not_count(self):
        self.assertFalse(srv.codex_fleet_state([self.acct("a", w=100), self.acct("b", w=5, stale=True)], 100)["exhausted"])
        st = srv.codex_fleet_state([self.acct("a", w=100), self.acct("b", w=5, auth="dead", stale=True)], 100)
        self.assertTrue(st["exhausted"])

    def test_fleet_alerts_are_edge_triggered_and_recovery_needs_positive_headroom(self):
        out = [self.acct("a", w=100)]
        srv.codex_fleet_check([self.acct("a", w=10)])             # baseline silently
        srv.codex_fleet_check(out); srv.codex_fleet_check(out)
        self.assertEqual([e for e, _ in self.fleet], ["codex_exhausted"])
        srv.codex_fleet_check([self.acct("a", w=100, stale=True)])   # unreadable: not a recovery
        self.assertEqual(len(self.fleet), 1)
        srv.codex_fleet_check([self.acct("a", w=3)])
        self.assertEqual([e for e, _ in self.fleet], ["codex_exhausted", "codex_recovered"])

    def test_the_claude_fleet_never_sees_codex_accounts(self):
        pol = {"threshold": 95.0, "hysteresis": 5.0, "cooldown": 300.0, "strategy": "best", "model": None}
        claude = srv.cswap_accounts_from_json(doc())
        self.assertEqual(srv.fleet_state(claude, pol)["usable"], 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python -m unittest test_collector.TestCodexAlerts`
Expected: FAIL (`_codex_fleet_last` missing).

- [ ] **Step 3: Write minimal implementation**

(a) Replace the body of `notifiable(rec)` (keep its docstring) with:

```python
    u = rec["u"]
    if rec.get("stale"): return False
    # Codex may legitimately report a single window (weekly only, when OpenAI drops the short
    # limit). notify_check treats windows independently and skips negatives, so one is enough.
    if rec.get("provider") == "codex": return u["s"] >= 0 or u["w"] >= 0
    return u["s"] >= 0 and u["w"] >= 0
```

(b) Add two entries to `_AUTH_TITLES`:

```python
                "codex_dead": "\U0001F534 ClaudeTV: Codex login dead (action needed)",
                "codex_recovered": "\U0001F7E2 ClaudeTV: Codex auth recovered",
```

(c) In `_auth_transitions`, replace the two branches so the Claude wording is untouched and Codex gets its own:

```python
    for rec in accts:
        dead, was = rec["auth"] == "dead", _alerted.get(rec["key"], False)
        codex = rec.get("provider") == "codex"
        who = "%s%s" % (rec["label"], (" (%s)" % rec["email"]) if rec["email"] else "")
        if dead and not was:
            _alerted[rec["key"]] = True
            if codex:
                _auth_alert("codex_dead", "OpenAI rejected the Codex login for %s. That account shows "
                            "LOGIN EXPIRED on the display until you log in again. Run this in your own "
                            "shell on the collector host, as the user the collector runs as: "
                            "CODEX_HOME=%s codex login --device-auth" % (who, rec.get("home", "~/.codex")))
            else:
                _auth_alert("dead", "Anthropic rejected the Claude login for %s. That account shows "
                            "LOGIN EXPIRED on the display until you log in again (cswap: log in with "
                            "that account and re-run `cswap add`; native: "
                            "python3 claude_usage_server.py --login)." % who)
        elif not dead and was:
            _alerted[rec["key"]] = False
            _auth_alert("codex_recovered" if codex else "recovered",
                        "%s is accepted again; the display is back to live data." % rec["label"])
```

(d) Replace `_reset_message` with a provider-aware version. The existing function contains em dash characters in three strings; this rewrite removes them (colon or comma instead), per the global constraint:

```python
_PROVIDER_NAMES = {"claude": ("Claude", "Anthropic"), "codex": ("Codex", "OpenAI")}

def _reset_message(kind, u, cls="expected", maxed=False, provider="claude"):
    name, vendor = _PROVIDER_NAMES.get(provider, _PROVIDER_NAMES["claude"])
    window = "session (5h)" if kind == "session" else "weekly (7d)"
    # a window the provider did not report is skipped, never printed as -1%
    parts = ["%s %d%%" % (k.upper(), u[k]) for k in ("s", "w") if u.get(k, -1) is not None and u.get(k, -1) >= 0]
    if u.get("f", -1) >= 0: parts.append("%s %d%%" % (u.get("fl") or "F", u["f"]))
    now = " · ".join(parts)
    nxt_v = u.get("sr") if kind == "session" else u.get("wr")
    nxt = (" Next reset %s%s." % ("~" if kind == "session" else "", nxt_v)) if nxt_v else ""
    if maxed:                                          # session that had hit its cap
        return ("%s Maxed %s session reset: you're unblocked" % ("\U0001F381" if cls == "gift" else "✅", name),
                "Your session hit its cap and just reset%s. Now: %s.%s"
                % (" EARLY, a gift!" if cls == "gift" else "", now, nxt))
    if cls == "gift":
        return ("\U0001F381 %s gift: %s %s usage reset early" % (vendor, name, window),
                "Your %s quota was reset ahead of schedule, free capacity. Now: %s.%s" % (window, now, nxt))
    return ("%s %s usage reset" % (name, window),
            "Your %s quota just refreshed. Now: %s.%s" % (window, now, nxt))
```

(e) `notify_check`: add the parameter and pass it through. Change the signature to `def notify_check(u, resets, acct="", label="", provider="claude"):` and change the one call `_reset_message(kind, u, cls, kind == "session" and _was_maxed(prev))` to `_reset_message(kind, u, cls, kind == "session" and _was_maxed(prev), provider)`.

(f) In `codex_apply_pending`, pass the provider: change the `notify_check(...)` call to end with `label=rec["label"], provider="codex")`, and replace the `globals().get("codex_fleet_check")` guard with a direct call:

```python
    f = codex_fleet_check(accts)
    with _lock: _codex["fleet"] = f
    return True
```

(g) Add two entries to `_FLEET_TITLES`:

```python
                 "codex_exhausted": "\U0001F6D1 ClaudeTV: every Codex account is out of quota",
                 "codex_recovered": "\U0001F7E2 ClaudeTV: Codex quota available again",
```

(h) In the Codex section, before `_codex = {...}`, add:

```python
# ---- codex fleet: is every Codex account out? There is no auto-switcher for Codex, so this is
# judged per account from the backend's own verdict first, a threshold second.
_codex_fleet_last = {}

def codex_fleet_state(accounts, threshold):
    rows, unknown = [], 0
    for rec in accounts:
        if rec.get("auth") == "dead" or rec.get("err") == "api_key": continue   # cannot help you
        if rec.get("stale"): unknown += 1; continue            # might be the one with room
        b = max(rec["u"].get("s", -1), rec["u"].get("w", -1))
        rows.append((rec["label"], b, bool(rec.get("blocked")) or b >= threshold))
    headroom = [lbl for lbl, _, out in rows if not out]
    return {"exhausted": bool(rows) and not headroom and not unknown, "headroom": headroom,
            "binding": {lbl: b for lbl, b, _ in rows}, "usable": len(rows), "unknown": unknown,
            "threshold": threshold}

def codex_fleet_check(accounts):
    """Edge-triggered, same rules as the Claude fleet: baseline silently, alert once when every
    account is out, and recover only on POSITIVE evidence of headroom."""
    thr = _cfg_num("CODEX_MAXED_THRESHOLD", 100)
    try:
        st = codex_fleet_state(accounts, thr)
        if not st["usable"]: return st
        was = _codex_fleet_last.get("exhausted")
        if st["exhausted"] and not was:
            _codex_fleet_last["exhausted"] = True
            worst = ", ".join("%s %d%%" % (l, b) for l, b in sorted(st["binding"].items()))
            _fleet_alert("codex_exhausted", "Every Codex account is out of quota, so Codex is blocked "
                         "until one resets. Now: %s." % worst)
            print("[%s] CODEX FLEET blocked (%s)" % (time.strftime("%H:%M:%S"), worst))
        elif was and st["headroom"]:
            _codex_fleet_last["exhausted"] = False
            _fleet_alert("codex_recovered", "%s has room again, so Codex is usable." % st["headroom"][0])
            print("[%s] CODEX FLEET recovered (%s)" % (time.strftime("%H:%M:%S"), st["headroom"][0]))
        elif was is None:
            _codex_fleet_last["exhausted"] = st["exhausted"]
        return st
    except Exception as e:
        print("[codex] fleet check error: %s" % e)
        return {"exhausted": False, "headroom": [], "binding": {}, "usable": 0, "unknown": 0, "threshold": thr}
```

- [ ] **Step 4: Run test to verify it passes**

Run `TestCodexAlerts`, then the full suite. Expected: PASS; `Ran 156 tests`, `OK`. The Claude fleet, reset and wire tests must be untouched and green.

- [ ] **Step 5: Commit**

```bash
rtk git add host/claude_usage_server.py host/test_collector.py
rtk git commit -m "codex: relogin alerts, per-provider reset wording and fleet verdict"
```

---

### Task 7: Wire field, master terminal, login helper, docs

**Files:**
- Modify: `host/claude_usage_server.py` (`usage_wire`, `TERMINAL`, `__main__`, new `codex_login`)
- Modify: `host/.env.example`, `host/install.sh`, `README.md`, `.gitignore`
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: records with `provider`; `full_state()["accounts"]["sources"]`, `["list"][i]` fields `provider, plan, blocked, home, buckets`; `codex_bin`, `CODEX_HOMES_ROOT`.
- Produces: `acc[i]["p"]` = `"c"` or `"x"`; `codex_login(alias)`; CLI flag `--codex-login <alias>`.

- [ ] **Step 1: Write the failing test**

```python
class TestCodexWireAndCli(CodexIsolated):
    def test_every_account_carries_its_provider_and_the_flat_keys_are_unchanged(self):
        claude = srv.cswap_accounts_from_json(doc())
        codex = srv.codex_account_from_rpc("default", "", "/h", cx_raw())
        w = srv.usage_wire(claude + [codex], {})
        self.assertEqual([a["p"] for a in w["acc"]], ["c", "c", "x"])
        self.assertEqual((w["n"], w["s"], w["w"], w["f"], w["fl"]), (3, 27, 6, 2, "FABLE"))

    def test_a_record_without_a_provider_is_claude(self):
        rec = dict(srv.cswap_accounts_from_json(doc())[0]); rec.pop("provider", None)
        self.assertEqual(srv.usage_wire([rec], {})["acc"][0]["p"], "c")

    def test_cswap_records_are_tagged_claude(self):
        self.assertEqual({a["provider"] for a in srv.cswap_accounts_from_json(doc())}, {"claude"})

    def test_the_payload_stays_small_enough_for_the_esp(self):
        eight = [srv.codex_account_from_rpc("h%d" % i, "account%d" % i, "/h", cx_raw()) for i in range(8)]
        self.assertLess(len(json.dumps(srv.usage_wire(eight, {}), separators=(",", ":"))), 1400)

    def test_codex_login_makes_a_private_home_and_execs_the_official_login(self):
        seen = {}; old = (srv.CODEX_HOMES_ROOT, subprocess.call)
        srv.CODEX_HOMES_ROOT = os.path.join(self.tmp.name, "codex")
        subprocess.call = lambda argv, env=None: seen.update(argv=argv, home=env["CODEX_HOME"]) or 0
        try:
            with self.assertRaises(SystemExit) as cm: srv.codex_login("Work")
        finally: srv.CODEX_HOMES_ROOT, subprocess.call = old
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(seen["argv"], ["codex", "login", "--device-auth"])
        self.assertTrue(seen["home"].endswith(os.path.join("codex", "Work")))
        self.assertTrue(os.path.isdir(seen["home"]))

    def test_codex_login_rejects_names_that_could_escape_the_root(self):
        for bad in ("", "default", "../x", "a/b"):
            with self.assertRaises(SystemExit) as cm: srv.codex_login(bad)
            self.assertEqual(cm.exception.code, 2)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL (`KeyError: 'p'`, `codex_login` missing, `KeyError: 'provider'`).

- [ ] **Step 3: Write minimal implementation**

(a) In `cswap_accounts_from_json`, add `"provider": "claude",` as the first key of the record dict passed to `recs.append({...})`.

(b) In `usage_wire`, replace the `st["acc"] = [...]` line with:

```python
    st["acc"] = [{"l": a["label"], "auth": a["auth"],
                  "p": "x" if a.get("provider") == "codex" else "c", **a["u"]} for a in accts]
```

(c) Add, above `if __name__ == "__main__":`:

```python
def codex_login(alias):
    """Convenience wrapper, run BY THE HUMAN in their own shell: make a private CODEX_HOME for
    one more Codex account and exec the official `codex login` in it. The service never calls
    this, and no credential ever passes through the collector."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", alias or "") or alias.lower() == "default":
        print("usage: claude_usage_server.py --codex-login <alias>   (letters, digits, - and _)")
        raise SystemExit(2)
    exe = codex_bin()
    if not exe:
        print("codex is not installed on this host. Install the Codex CLI, then re-run."); raise SystemExit(1)
    home = os.path.join(CODEX_HOMES_ROOT, alias)
    os.makedirs(home, mode=0o700, exist_ok=True)
    try: os.chmod(home, 0o700)
    except OSError: pass
    print("Logging a Codex account into %s\nLog in separately on every machine: never copy this "
          "directory from another box, refresh tokens rotate.\n" % home)
    raise SystemExit(subprocess.call([exe, "login", "--device-auth"], env=dict(os.environ, CODEX_HOME=home)))
```

and in `__main__`, directly after the `--login` branch:

```python
    if "--codex-login" in sys.argv:
        i = sys.argv.index("--codex-login")
        codex_login(sys.argv[i + 1] if i + 1 < len(sys.argv) else "")
```

Note for the test: `srv.codex_bin` is stubbed to return `"codex"` by `CodexIsolated`, which is why the expected argv starts with `"codex"`.

(d) `TERMINAL` JavaScript. Replace the line that starts with ` pill(src,cs?'ok':'bad',` and the line after it (which sets `srcerr.textContent`) with:

```javascript
 const S=A.sources||{claude:{n:A.list.length},codex:{n:0}},nc=(S.claude||{}).n||0,nx=(S.codex||{}).n||0;
 pill(src,cs?'ok':'bad',cs?((nc?('claude-swap '+nc):'')+(nc&&nx?' + ':'')+(nx?('codex '+nx):'')):'setup needed');
 srcerr.textContent=(nc?((A.cswap_ver||'cswap')+' at '+A.cswap):((nx?'':'! ')+(A.source_err||'claude-swap not ready')))
   +(S.codex&&S.codex.bin?(' | '+(S.codex.ver||'codex')+', polled '+fmtAgo(S.codex.age)+', next in '+S.codex.next_in+'s'):'');
```

In the account row template, change the label span so it carries a provider badge, the plan and a blocked marker. The label sits in the middle of a longer string literal, so the anchor starts at `<span>` with no leading quote. Replace the exact text `<span><b>'+a.label+'</b>'+(a.active?' <span class=muted>· active</span>':'')` with:

```javascript
<span><b>'+a.label+'</b> <span class=muted>'+(a.provider=='codex'?'codex':'claude')+(a.plan?(' '+a.plan):'')
      +'</span>'+(a.blocked?' <span style=color:#f0ad36>blocked</span>':'')+(a.provider!='codex'&&a.active?' <span class=muted>active</span>':'')
```

and append a buckets line for Codex rows by replacing the final `+'</span></div></div>';}).join('')` of the template with:

```javascript
+'</span></div>'
    +((a.buckets||[]).length>1?('<div class=muted style="margin-top:2px">'+a.buckets.map(b=>(b.name||b.id)+': '
      +b.windows.map(w=>pc(w.pct)+'/'+(w.mins>=1440?Math.round(w.mins/1440)+'d':(w.mins?Math.round(w.mins/60)+'h':'?'))).join(' ')).join(' | ')+'</div>'):'')
    +'</div>';}).join('')
```

A dead Codex row should say what to run. The `LOGIN EXPIRED` pill itself stays as it is. Add one line to the template, after the email row and before the resets row:

```javascript
    +(dead&&a.provider=='codex'?('<div class=muted>run on this host: <code>CODEX_HOME='+a.home+' codex login --device-auth</code></div>'):'')
```

`TERMINAL` HTML. After the closing `</div>` of the `acchelp` block, add a Codex help block and its filter input (keep them inside the Accounts card, before the `CSWAP_ACCOUNTS` label):

```html
<button class=ghost style="margin-top:8px" onclick="document.getElementById('cxhelp').style.display=''">How to add a Codex account</button>
<div id=cxhelp class=muted style="display:none;margin-top:8px;line-height:1.6">
Codex accounts are read through the official <b>codex</b> CLI, one folder per account. The login on this
host in <code>~/.codex</code> is picked up automatically. For each extra account run, in your own shell:<br>
<code>python3 ~/.claudetv/claude_usage_server.py --codex-login &lt;name&gt;</code><br>
The name becomes the label on the display (first 8 characters). ClaudeTV never sees the token.<br><br>
<b>Log in separately on every machine.</b> Never copy a Codex folder from another box: refresh tokens
rotate, and the first machine to refresh invalidates the other.
</div>
<label style="margin-top:10px" for=CODEX_ACCOUNTS>Show only these Codex accounts (blank = all; comma list of folder names, use default for ~/.codex)</label>
<input id=CODEX_ACCOUNTS placeholder="e.g. default,work">
```

In `saveCfg`, add `'CODEX_ACCOUNTS'` to the `ks` array, right after `'CSWAP_ACCOUNTS'`.

(e) `host/.env.example`: after the `CLAUDETV_CSWAP_ACCOUNTS` block add (comments on their own lines, never after a value: the loader keeps everything after `=`):

```
# --- Codex accounts (optional) --------------------------------------------------------------
# Read through the official `codex` CLI: one CODEX_HOME folder per account, and ClaudeTV never
# holds a Codex token. ~/.codex is picked up automatically. Add more with:
#     python3 claude_usage_server.py --codex-login <name>
# blank = auto: ~/.local/bin/codex, else codex on PATH
CLAUDETV_CODEX_BIN=
# blank = all; else a comma list of folder names that filters AND orders (default = ~/.codex)
CLAUDETV_CODEX_ACCOUNTS=
# seconds between Codex polls; 300 is the enforced minimum (one live request per account)
CLAUDETV_CODEX_EVERY=300
# hard deadline for one Codex read; Codex itself never abandons a hung backend
CLAUDETV_CODEX_TIMEOUT=20
# blank = off; else part of a model limit's name to show as the third number (e.g. spark)
CLAUDETV_CODEX_SCOPED=
# an account counts as out at this percent on its worst window (or when OpenAI says blocked)
CLAUDETV_CODEX_MAXED_THRESHOLD=100
```

(f) `host/install.sh`: after the block that reports the claude-swap account count (the `if [ "$("$CSWAP" list --json ...` block), add:

```bash
# Codex is optional: ClaudeTV reads it through the official CLI and never installs it for you.
CODEX="$(command -v codex || true)"; [ -x "$HOME/.local/bin/codex" ] && CODEX="$HOME/.local/bin/codex"
if [ -n "$CODEX" ]; then
  c "OK codex: $("$CODEX" --version 2>/dev/null || echo present)" ok
  [ -f "$HOME/.codex/auth.json" ] && c "   ~/.codex is logged in and will show on the display" ok
  c "   add another Codex account:  python3 $DEST/claude_usage_server.py --codex-login <name>" info
fi
```

(g) `.gitignore`: under `**/credentials.json` add `**/auth.json`.

(h) `README.md`: add a section after "Multiple accounts", titled `## Codex accounts`, covering in this order: what it shows (the same card, cycling alongside Claude accounts); that it needs the `codex` CLI on the collector host and nothing else; that `~/.codex` is automatic and extra accounts are `--codex-login <name>`; the separate-login warning; that plans with only a weekly limit show `--` for the session number; the six `CLAUDETV_CODEX_*` keys; that a dead login alerts on your channels and shows `LOGIN EXPIRED` on that account's card; the honest note that this reads a private OpenAI endpoint through the official client at one request per account per five minutes; and a Pi note (the `codex` binary is a 263 MB static executable, so the first read after boot can take several seconds on an SD card; `CLAUDETV_CODEX_TIMEOUT` covers it). Update the "How it works" diagram caption to say accounts come from cswap and, optionally, codex. No em dashes.

- [ ] **Step 4: Run test to verify it passes**

Run `TestCodexWireAndCli`, then the full suite. Expected: PASS; `Ran 162 tests`, `OK`.
Then load the terminal once to catch a JavaScript syntax error, which no unit test sees:

```bash
cd host && CLAUDETV_PORT=8097 rtk python claude_usage_server.py &
rtk curl -s localhost:8097/api/state | rtk python -c "import json,sys; s=json.load(sys.stdin)['accounts']; print(s['sources']); print([(a['label'],a['provider']) for a in s['list']])"
```

Open `http://localhost:8097/` in a browser, confirm the Accounts card renders both providers and the console shows no errors, then stop the process.

- [ ] **Step 5: Commit**

```bash
rtk git add host/ README.md .gitignore
rtk git commit -m "codex: provider field on the wire, terminal rows, --codex-login helper, docs"
```

---

### Task 8: Contract drift test and live acceptance on the host

**Files:**
- Test: `host/test_collector.py`

**Interfaces:**
- Consumes: `codex_bin`, `codex_rpc_read`, `codex_account_from_rpc`, `codex_status`.
- Produces: `TestCodexLiveContract`, skipped unless `CLAUDETV_LIVE_CODEX=1` and a logged-in `codex` exist.

- [ ] **Step 1: Write the test**

```python
@unittest.skipUnless(os.environ.get("CLAUDETV_LIVE_CODEX") == "1" and srv.codex_bin()
                     and os.path.isfile(os.path.join(srv.CODEX_DEFAULT_HOME, "auth.json")),
                     "set CLAUDETV_LIVE_CODEX=1 on a host with a logged-in codex")
class TestCodexLiveContract(TzPinned):
    """Codex marks app-server experimental. This pins the parts of its contract we depend on,
    against the INSTALLED binary, so an upgrade that breaks us fails here and not on the desk."""

    def test_the_schema_still_has_the_methods_and_fields_we_read(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([srv.codex_bin(), "app-server", "generate-json-schema", "--out", d],
                           capture_output=True, timeout=60, check=True)
            blob = ""
            for root, _, files in os.walk(d):
                for f in files:
                    if f.endswith(".json"):
                        with open(os.path.join(root, f), encoding="utf-8") as fh: blob += fh.read()
        for needle in ("account/rateLimits/read", "account/read", "excludeResetCreditDetails",
                       "rateLimitsByLimitId", "usedPercent", "windowDurationMins", "resetsAt",
                       "ordinaryUsageAllowed", "rateLimitReachedType", "planType"):
            self.assertIn(needle, blob, "codex app-server no longer exposes %r" % needle)

    def test_a_real_read_maps_cleanly_inside_the_deadline(self):
        t0 = time.monotonic()
        raw = srv.codex_rpc_read(srv.codex_bin(), srv.CODEX_DEFAULT_HOME, 20)
        wall = time.monotonic() - t0
        rec = srv.codex_account_from_rpc("default", "", srv.CODEX_DEFAULT_HOME, raw)
        self.assertEqual((rec["auth"], rec["stale"]), ("ok", False), rec["err"])
        self.assertTrue(rec["u"]["s"] >= 0 or rec["u"]["w"] >= 0)
        self.assertLess(wall, 10.0, "a real read took %.1fs" % wall)

    def test_a_home_with_no_login_is_dead_not_transient(self):
        with tempfile.TemporaryDirectory() as d:
            raw = srv.codex_rpc_read(srv.codex_bin(), d, 20)
        self.assertEqual(srv.codex_status(raw["account"], raw["limits"], raw["driver_err"]),
                         ("dead", "login_required"))
```

- [ ] **Step 2: Run it live on the dev machine**

Run from `host/`: `CLAUDETV_LIVE_CODEX=1 rtk python -m unittest test_collector.TestCodexLiveContract -v`
Expected: 3 tests PASS. Without the variable: 3 skipped, and the full suite reports `Ran 165 tests`, `OK (skipped=3)`.

- [ ] **Step 3: Commit**

```bash
rtk git add host/test_collector.py
rtk git commit -m "codex: opt-in contract test against the installed app-server"
```

- [ ] **Step 4: Live acceptance on the production host, beside production, without touching it**

The production collector runs from `/home/a/ClaudeTV/host/` via a symlink and systemd. This step runs the branch from a temp directory on another port, so production is never restarted or modified. Its notifier files land in the temp directory, so no production alert state is touched. It shares only `cswap list --json` reads (harmless) and adds Codex reads that production does not make yet.

```bash
rtk git archive --format=tar HEAD host | ssh ubuntu 'set -e; d=$(mktemp -d /tmp/ctv-accept.XXXXXX); tar xf - -C "$d"; cd "$d/host"; \
  python3 -m unittest test_collector 2>&1 | tail -3; \
  CLAUDETV_LIVE_CODEX=1 python3 -m unittest test_collector.TestCodexLiveContract 2>&1 | tail -3; \
  (CLAUDETV_PORT=8099 timeout 90 python3 -u claude_usage_server.py > run.log 2>&1 &) ; sleep 45; \
  curl -s -m 5 localhost:8099/usage | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"n=%s acc=%s\" % (d[\"n\"], [(a[\"p\"], a[\"auth\"], a[\"s\"], a[\"w\"]) for a in d[\"acc\"]]))"; \
  curl -s -m 5 localhost:8099/api/state | python3 -c "import json,sys; print(json.load(sys.stdin)[\"accounts\"][\"sources\"])"; \
  sleep 40; grep -i -E "codex|error|Traceback" run.log | head; cd /; rm -rf "$d"; pgrep -u $(id -u) -af "[c]odex app-server" | wc -l'
```

Expected, all of:
- the full suite is `OK` on Python 3.12 (the dev machine runs 3.14);
- the live contract tests pass on the host's codex;
- `n` equals the number of cswap accounts plus 1, the last `acc` entry has `p == "x"`, `auth == "ok"`, `s == -1` and a `w` that matches `/status` in the Codex TUI on that box;
- `sources.codex` shows the binary path, a version, `n: 1`;
- the log has no traceback;
- the final number is `0`: no `codex app-server` process left behind.

Look at the clock while it runs. The Codex READ happens within a couple of seconds of start (`_codex["next"]` starts at 0), but it is APPLIED on the poller thread, so the account shows up once the first Claude iteration finishes (about 15 s in the dry run, bounded by cswap's 45 s timeout). That ordering is deliberate: it keeps notifier state single-threaded. It is a failure only if the Codex account is still missing after a minute, or if it waits for the first 300 s interval.

- [ ] **Step 5: Stop here**

Do not deploy. Production deploy is `git pull` in `/home/a/ClaudeTV` plus `sudo systemctl restart claude-usage`, which restarts a live service and is the owner's call. Report the acceptance output and wait. Use superpowers:finishing-a-development-branch to decide how the branch is integrated.

## Self-review notes (already applied)

- Spec coverage: source seam (Task 5), discovery and login model (Tasks 3, 7), driver (Task 4), mapper and window rules (Task 2), status vocabulary including the 401 rule (Task 1), scheduling, floor, backoff and dead hold (Task 5), reset detection, relogin alerts and fleet (Task 6), wire field and terminal (Task 7), config, installer, docs (Tasks 3, 7), testing section including the wall-clock budget, isolation, contract drift and live acceptance (Tasks 4, 5, 8). Firmware is out of scope per the spec.
- The device-side relogin notice needs no firmware change: it rides the existing per-account `auth: "dead"` takeover, asserted in Task 6.
- Names are consistent across tasks: `codex_status`, `codex_windows`, `codex_account_from_rpc`, `codex_with_last_good`, `codex_bin`, `codex_version`, `codex_homes`, `_cfg_num`, `codex_rpc_read`, `codex_poll`, `_publish`, `_src_accts`, `_codex`, `codex_refresh`, `codex_next_delay`, `codex_io_loop`, `codex_apply_pending`, `codex_fleet_state`, `codex_fleet_check`, `_codex_fleet_last`, `codex_login`.
- Expected test counts are cumulative from the 88-test baseline: 90, 109, 119, 129, 141, 156, 162, 165. If a count differs, a test was dropped or duplicated: find out which before moving on.
