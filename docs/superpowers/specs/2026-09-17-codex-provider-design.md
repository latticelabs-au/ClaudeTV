# Codex as a second usage provider: design

Date: 2026-09-17. Status: approved and implemented on `feat/codex-provider`. Scope: collector (`host/`), plus firmware v5.2 and the emulator for the provider tint and the weekly-only card.

## Goal

Show OpenAI Codex (ChatGPT-plan) usage on the same display and master terminal as Claude usage,
for one or several Codex accounts, without weakening any guarantee the Claude path has today:

- the collector holds no provider token and runs no OAuth at runtime;
- a failing source never blanks a healthy one, and the device always gets last-good numbers;
- only a login that genuinely needs a human flips a card to `LOGIN EXPIRED`;
- a missing number is `-1`, never `0`, so nothing can fake a reset;
- a v5.1 device keeps working against the new collector with no reflash.

Non-goals for this version: switching Codex accounts (there is no `cswap auto` equivalent and
we are not building one), API-key Codex logins (they have no subscription limits), token or
cost accounting from logs.

## Decision: two sources, and the Codex source is Codex itself

Research (8 agents, two Opus verifiers, 92 claims judged) found no single middleware that can
replace cswap for both providers. cswap is the only tool that refreshes idle Claude seats
itself, which is what makes it work on a box that never runs Claude Code. caam states it
cannot refresh Claude tokens; CodexBar's multi-account Claude path shells out to
`cswap --list --json`, so it is a cswap consumer; claude-swap's own Codex request (issue 252)
has been open since 2026-08-16 with no reply.

So Claude stays on cswap, untouched, and Codex gets its own source. For that source we use the
official binary: `codex app-server` over stdio, JSON-RPC method `account/rateLimits/read`,
one `CODEX_HOME` directory per account.

Why this over a third-party Codex tool (`ndycode/codex-multi-auth`, `maddada/codex-swap`):

- `codex` must be on the host anyway, because it is how you log in.
- Codex's own auth manager refreshes the token on the read path (when the access token is
  within 5 minutes of expiry, or 8 days since the last refresh), so polling is the keep-alive
  and exactly one component ever rotates a given credential home. The collector never opens
  `auth.json`.
- No extra runtime (codex-multi-auth needs Node and serves a cache unless told `--refresh`)
  and no abandonment risk from a small single-maintainer project (codex-swap has 5 stars and
  does not refresh at all).
- The source sits behind one function, so moving to another backend later is a contained change.

Known risk: `app-server` is labelled experimental in the CLI help. It is also the surface the
Codex IDE extension and desktop app are built on, the binary can emit its own JSON Schema
(`codex app-server generate-json-schema`), and every field we read is nullable-tolerant in
our mapper. A contract test pins the shape (see Testing).

Rejected mechanisms: direct `GET chatgpt.com/backend-api/wham/usage` from `urllib` (would make
the collector read the token and, on an idle box, own the refresh: exactly the second-keeper
hazard v5.1 removed for Claude; bare-urllib acceptance by Cloudflare is also unverified);
rollout JSONL parsing (often null per openai/codex issue 14880, only updates when Codex runs
on that machine, zstd-compressed after 7 days); response headers and SSE events (only arrive
on model calls, which spend tokens).

## Evidence from the spike

Measured on the production host (`ssh ubuntu`: Ubuntu 24.04, codex-cli 0.154.0, systemd-like
stripped environment, no TTY) and cross-checked on Windows. Throwaway scripts, deleted.

| Measure | Result | Consequence |
|---|---|---|
| Cold poll: spawn, handshake, read, exit | median 1.09 s, p95 1.40 s | spawn per poll is affordable |
| Warm long-lived server | 0.67 s per read, 93 MB resident, 44 threads | not worth keeping resident |
| Cost per cold poll | about 0.56 CPU-seconds, 86 MB peak RSS, transient | negligible at a 5 minute cadence |
| 8 concurrent cold polls (firmware `MAXACC`) | 8/8 ok, 1.15 s wall | poll accounts concurrently |
| Default launch | 7 backend requests (plugin catalogue) | always launch with `--disable plugins`: exactly 1 request |
| Backend hangs | Codex never times out (waited 75 s) | the driver owns the deadline and the kill |
| stdin closed | server exits by itself in 0.03 s, rc 0 | shut down by closing stdin, kill only as fallback |
| Orphan processes after 25 launches | 0 | no process-group gymnastics needed, but spawn in a new session anyway |
| Logged-out home | `-32600 "codex account authentication required to read rate limits"` | the only `dead` signal |
| Refused / HTTP 500 / HTTP 429 / non-JSON body | `-32603 "failed to fetch codex rate limits: ..."` | all transient; 429 is recognisable in the message |
| Quota at the start vs end of every run (57 live polls over two machines) | unchanged within each run | polling is free |
| `auth.json` rewritten, session files created | no, none | read path has no credential side effects while the token is fresh |
| Log growth in `CODEX_HOME` | about 650 bytes per launch of real DB growth | ignorable |

Shape facts that constrain the mapper:

- The reply carries `rateLimits` (main bucket) and `rateLimitsByLimitId` (main bucket `codex`
  plus model buckets such as `codex_bengalfox` = "GPT-5.3-Codex-Spark"). Each bucket has
  `primary` and `secondary`, each `{usedPercent, windowDurationMins, resetsAt}` with
  `resetsAt` in unix seconds. Everything except `usedPercent` may be null, including the
  slots themselves.
- `primary` and `secondary` are transport slots, not meanings. The test account (`prolite`)
  currently reports only a weekly window (10080 min) and it sits in `primary` with
  `secondary: null`. Windows are classified by duration, never by slot.
- The reply also carries `ordinaryUsageAllowed` and `rateLimitReachedType`, a
  provider-authoritative "you are blocked" signal.

## Architecture

All changes are in `host/claude_usage_server.py` and `host/test_collector.py`, following the
file's existing single-module, stdlib-only style. New code is one section, "accounts: codex
source", placed after the cswap section.

### 1. The source seam

Today `fetch_accounts()` reads cswap and raises when it is missing or empty. It becomes a
merge over independent sources:

```
SOURCES = ("claude", "codex")
_src = {name: {"accts": [], "ts": 0, "err": "", "next": 0.0, "backoff": N} for name in SOURCES}
```

- The Claude source is today's `cswap_accounts()` on today's `USAGE_EVERY` timer, unchanged,
  including its "missing or empty raises and shows a setup state" behaviour.
- The Codex source runs on its own daemon thread with its own cadence, writing into
  `_src["codex"]` under `_lock`. It is fully optional: no `codex` binary or no Codex homes
  means an empty list and no error banner, so a Claude-only install behaves exactly as today.
- `_accounts` (what `/usage` and the terminal read) is the concatenation: Claude records
  first in cswap order, then Codex records in discovery order. Each source keeps its own
  last-good list, so a Codex failure leaves Claude cards live and vice versa.
- The existing per-poll passes (`_auth_transitions`, `notify_check`, fleet check) run per
  source right after that source refreshes, on that source's records only.
- The Codex thread only reads and maps. Its finished pass is handed to the poller thread,
  which publishes it and runs the stateful passes, so notifier state and its files stay
  single-threaded with no new locking. The cost, measured in the dry run: a Codex result is
  applied at the poller's next tick, so it can wait out one in-flight cswap call (about 15 s
  at startup, bounded by cswap's 45 s timeout). At a 5 minute cadence that is irrelevant, and
  the direction that matters holds: a hung Codex never delays Claude.
- A Codex-only host (no cswap) works: the device gets the Codex accounts, the lead account
  for the flat keys is the first Codex one, and the terminal shows the cswap setup hint as
  information rather than as an error whenever at least one source has accounts.

Every record gains `"provider": "claude" | "codex"`. `cswap_accounts_from_json` sets
`"claude"`; nothing else about Claude records changes, including their `key`, so
`notify_state.json` needs no migration.

### 2. Codex account discovery

`codex_homes()` returns an ordered list of `(alias, path)`:

1. `~/.codex` if it contains `auth.json` (alias from the account email's local part, else `CODEX`);
2. each subdirectory of `~/.claudetv/codex/` that contains `auth.json`, alias = directory name.

`CODEX_ACCOUNTS` (comma list of directory names, plus the literal `default` for `~/.codex`)
filters and orders, mirroring `CSWAP_ACCOUNTS`. Blank means all. It matches names rather
than emails on purpose: the filter is applied before polling, so a filtered-out account
costs no backend request. Presence of `auth.json` is only a discovery hint; the file is never
opened or parsed by the collector.

Adding an account is `CODEX_HOME=~/.claudetv/codex/<alias> codex login --device-auth`. A
convenience flag, `claude_usage_server.py --codex-login <alias>`, creates the directory
(mode 0700) and execs exactly that, so the README needs one line. The README repeats the
existing warning: log in separately on every machine, never copy a credential home.

`codex_bin()` resolves `CODEX_BIN` from config, then `~/.local/bin/codex`, then
`shutil.which("codex")`. The explicit `~/.local/bin` probe matters: the deployed systemd unit
has a bare PATH.

### 3. The RPC driver

`codex_rpc_read(exe, home, deadline_s)` is the only code that talks to Codex:

- `Popen([exe, "app-server", "--disable", "plugins"], env={..., "CODEX_HOME": home}, cwd="/",
  start_new_session=True)`, stdin and stdout pipes, stderr discarded.
- Send `initialize` (clientInfo name `claudetv`), then the `initialized` notification, then
  `account/read {"refreshToken": false}` and
  `account/rateLimits/read {"excludeResetCreditDetails": true}`. The flag halves backend
  requests; `supportsLunaReserve` is never sent (Codex source: passive readers must not opt in).
- A reader thread collects replies by id and ignores everything else, including unsolicited
  notifications (`remoteControl/status/changed` was observed).
- One absolute deadline covers the whole exchange (default `CODEX_TIMEOUT` = 20 s, 15x the
  measured p95). On deadline or EOF the result is a driver error, never an exception that
  escapes the source thread.
- Shutdown: close stdin, wait up to 2 s, then `killpg` as the fallback. Always reaps.
- Returns `(account_doc, limits_doc_or_error)`; no parsing beyond JSON.

All homes are read concurrently (one short-lived thread each), so a poll of 8 accounts costs
about the slowest single read, and a hung backend costs `CODEX_TIMEOUT`, not 8 times that.

### 4. The mapper (pure function)

`codex_account_from_rpc(alias, account_doc, limits_doc)` returns the standard record. It is
pure and is where most tests live, mirroring `cswap_accounts_from_json`.

Window classification, over the main bucket (`rateLimitsByLimitId["codex"]`, falling back to
`rateLimits`), across both slots:

- `W` = the window whose `windowDurationMins` is at least 1 day (1440), choosing the one
  closest to 10080 if several. A 3-day window is better shown under `W` with its real reset
  date than dropped while it is limiting you;
- `S` = the shortest window under 1 day (1440);
- a lone window with a null duration maps to `W` (the weekly is the one that persists when
  OpenAI drops the short limit); two windows with null durations fall back to
  primary = `S`, secondary = `W`. Both are documented heuristics with tests.

Record fields:

| Field | Value |
|---|---|
| `provider` | `"codex"` |
| `key` | `"codex:" + slot`, where slot is `default` for `~/.codex` or the directory name. Derived from the home, never from a reply, so it is identical on good and failed polls (the label-keyed instability that v5.1 fixed for cswap must not come back). If the `accountId` seen in a home changes (someone logged that home into a different account), that key's reset state is dropped so the first reading baselines silently |
| `label` | `_label(alias, email, "")`, same 8-char cap |
| `email` | from `account/read` (shown in the terminal only) |
| `u.s`, `u.w` | rounded `usedPercent`, `-1` when that window is absent |
| `u.sr`, `u.wr` | `resetsAt` rendered with the existing `_clock` / `_clock_short` formatters, so card geometry still fits |
| `u.f`, `u.fl` | `-1`, `""` by default (see below) |
| `resets` | `{session, week}` as ISO 8601 UTC strings, converted from unix seconds, because the notifier compares ISO values |
| `active` | `True`; `disabled` `False` (no rotation concept) |
| `blocked` | `True` when `ordinaryUsageAllowed is False` or `rateLimitReachedType` is set |
| `plan` | `planType`, terminal only |
| `buckets` | every bucket with its windows, terminal only |
| `age` | seconds since this account's last good read |

Model-scoped buckets are not sent to the device by default: on the test account the only one
is a model the user does not run, and its column letter would collide ("S" for Spark next to
"S" for session). `CODEX_SCOPED` (blank by default) may name a bucket id or name substring to
put into `f` / `fl`; all buckets are always visible in the master terminal.

### 5. Status vocabulary

Follows the project's original rule (commit 56806bc): a login is `dead` when the USAGE
endpoint rejects it, never because a refresh failed, and 429 is never dead. Read from Codex
source at commit b0659c5: when a refresh fails permanently (`refresh_token_expired`,
`refresh_token_reused`, `refresh_token_invalidated`, `invalid_grant`), `AuthManager::auth()`
logs it and still returns the stale credential, `account/read` keeps reporting the account
from disk, and the usage call goes out with the dead access token. So a login that needs a
human surfaces as `-32603 "failed to fetch codex rate limits: GET .../wham/usage failed:
401 Unauthorized; ..."`, not as `-32600`.

| Observation | `auth` | `err` | `stale` |
|---|---|---|---|
| good reply | `ok` | `""` | `False` |
| `account/read` returns `account: null`, or limits error code `-32600` (no login in this home) | `dead` | `login_required` | `True` |
| error `-32603`, message carries HTTP `401` from the usage endpoint (login rejected) | `dead` | `login_expired` | `True` |
| error `-32603`, HTTP `403` with a non-HTML body | `dead` | `login_expired` | `True` |
| error `-32603`, HTTP `403` with `content-type=text/html` (a Cloudflare challenge, not an auth verdict) | `ok` | `blocked_by_edge` | `True` |
| account type is an API key | `ok` | `api_key` (no subscription quota) | `True` |
| error `-32603`, message contains `429` | `ok` | `rate_limited` | `True` |
| any other `-32603` (refused, 5xx, decode) | `ok` | `unavailable` | `True` |
| driver deadline, EOF, spawn failure, unparseable output | `ok` | `timeout` / `unavailable` | `True` |
| any error code not listed | `ok` | `unknown` | `True` |

A stale Codex record keeps its last-good `u` for display (per-account last-good cache inside
the source), exactly as cswap's `lastGoodUsage` does.

A `dead` Codex account gets the same treatment a dead Claude account gets today, with no new
machinery: `_auth_transitions` fires one edge-triggered alert per outage on every enabled
channel (email, Discord, Slack, gated by `NOTIFY_AUTH`), names the account and the exact
remedy (`CODEX_HOME=<path> codex login --device-auth`), and fires one recovery alert when the
account reads cleanly again. On the device, `auth: "dead"` in that account's `acc[]` entry
takes over only that account's card with the existing `LOGIN EXPIRED / re-auth on host`
screen while the other accounts keep cycling. This already works on v5.1 firmware.

The HTTP status is parsed out of the error message with one anchored pattern
(`failed: (\d{3}) `), because Codex gives no structured status. That string is the fragile
part of this design, so it has its own tests and the contract-drift test covers it.

### 6. Scheduling

- `CODEX_EVERY`, default and minimum 300 s. Each Codex read is a live backend request per
  account (unlike cswap, which refreshes one stale account per call), and the endpoint is
  private, so we stay slow on purpose.
- A failure never makes the source poll faster than `CODEX_EVERY`: one account failing must
  not drag the healthy ones below the floor. Failures only slow it down. When every account
  failed (a source-wide outage) the delay doubles per consecutive failure, capped at 30 min.
  Any `rate_limited` account starts that doubling at 15 min.
- A `dead` account is re-read only every 30 min while the others keep their cadence: every
  read of a dead home makes a fresh Codex process retry a refresh that cannot succeed, and
  polling cannot fix a login. "Re-read accounts" overrides the hold, so a re-login shows up
  on demand.
- The terminal's "Re-read accounts" button forces both sources, but the Codex source ignores
  a force that arrives less than 60 s after its previous poll.

### 7. Reset detection and alerts

- `notifiable(rec)` today requires both `s` and `w`. It becomes: fresh, and `w >= 0`, and
  for Claude additionally `s >= 0` (unchanged behaviour for Claude). A weekly-only Codex
  account therefore still gets week-reset detection. `notify_check` already treats windows
  independently and skips negative values, so a short window that disappears is not a drop.
- State is namespaced by `key`, so `codex:` keys cannot collide with cswap's `slot:email`.
- Alert text names the provider. `_AUTH_TITLES`, `_reset_message` and the fleet titles take a
  provider display name; Claude wording stays byte-identical so existing tests keep passing.
  The Codex dead-login remedy reads `CODEX_HOME=<path> codex login --device-auth`.

### 8. Fleet ("you are actually blocked") per provider

`fleet_check` is evaluated per provider with separate edge-trigger memory:

- Claude: unchanged in every respect (cswap's `autoswitch.threshold`, benched-account naming).
- Codex: an account is out when `blocked` is true, or when its binding window (max of `s`,
  `w`) reaches `CODEX_MAXED_THRESHOLD` (default 100). The fleet is exhausted when every
  readable Codex account is out and none is unknown, same rule shape as Claude. Alerts reuse
  `NOTIFY_FLEET_MAXED` and the recovery rule (positive evidence of headroom).
- No cross-provider "everything is out" alert. Two provider alerts already say it.

### 9. Wire format

`acc[]` entries gain `"p": "c" | "x"`. Nothing else changes: flat keys still mirror the lead
account, `n` counts all accounts, `?acct=<label>` still pins one. A v5.1 device ignores `p`
and cycles a Codex account by its label with the classic two-column card (`SESSION --` when
there is no short window). Firmware v5.2, added on the same branch at the owner's request: Codex accounts are violet (header
square, label, page dots), a lone Codex account is titled `CODEX USAGE`, and a Codex account with
a weekly limit only gets a single-hero WEEK card instead of a dead `SESSION --` column. The
classic two-column card also drops the minutes from the week reset when the two reset times
would touch, which Codex's odd-minute resets made likely. `?acct=` additionally matches the
record key (`codex:work`), the unambiguous pin when two providers share a label.
`MAXACC` stays 8 across both providers; the terminal warns when the merged list exceeds it.

### 10. Master terminal, config, installer, docs

- Accounts card: a provider badge per row, plan, every Codex bucket, per-source health line
  (backend, version, last poll, error, next poll), and a "How to add a Codex account" guide
  next to the cswap one. `full_state()["accounts"]` gains `sources: {claude: {...}, codex: {...}}`.
- New `EDITABLE` keys: `CODEX_BIN`, `CODEX_ACCOUNTS`, `CODEX_EVERY`, `CODEX_TIMEOUT`,
  `CODEX_SCOPED`, `CODEX_MAXED_THRESHOLD`. All optional with working defaults.
- `install.sh`: detect `codex`, print the add-account one-liner, never install Codex itself.
- `.env.example` and README: a "Codex accounts" section, the separate-login warning, and the
  honest note that this reads a private endpoint through the official client.

## Error handling summary

Nothing in the Codex source may raise into the poller, block the Claude source, or write `0`
where a number is unknown. Every failure degrades to: last-good numbers, `stale`, a reason
string in the terminal, and backoff. The only user-visible alarm states are `LOGIN EXPIRED`
(no login in the home, or the usage endpoint answering 401 / a non-HTML 403) and the provider
fleet alert. A dead account backs off to a 30 minute retry, since polling cannot fix it, and
the first clean read after a re-login clears it and sends the recovery alert.

## Testing

`host/test_collector.py`, stdlib `unittest`, TDD per task.

- **Mapper fixtures from real captures:** weekly-only `prolite` (weekly in the primary slot),
  short plus weekly, slots swapped, null durations, null `resetsAt`, missing `codex` bucket,
  multi-bucket, `CODEX_SCOPED` on and off, `blocked` variants, API-key account, reset strings
  in the device format, ISO `resets`, key stability.
- **Status table:** one test per row of section 5, including "unknown error code is never dead".
- **Driver against a fake `codex`:** a small Python script speaking the same stdio protocol,
  covering: happy path, chatter before and between replies, auth-required error, garbage
  lines, early exit, and a server that never answers. Asserts the launch argv contains
  `--disable plugins`, `CODEX_HOME` is set per account, and the child is reaped.
- **Largest plausible input with a wall-clock budget:** 8 homes where every fake server
  hangs. With `CODEX_TIMEOUT` = 2 s in the test, the whole poll must finish in under 4 s of
  measured time and leave zero child processes. This encodes "Codex never times out" and
  "accounts are read concurrently" as a clock assertion, not a note.
- **Source isolation:** Codex source raising or hanging leaves Claude records and wire output
  byte-identical to a Claude-only run; a Claude failure leaves Codex cards live.
- **Notifier and fleet:** weekly-only account produces a week reset and no phantom session
  reset; a vanished short window is not a reset; Codex keys never touch Claude state; Codex
  fleet blocks on `blocked` and on threshold, recovers only on positive headroom; Claude fleet
  tests untouched and green.
- **Wire contract:** `p` present, flat keys unchanged, v5.1 parsing assumptions still hold.
- **Contract drift (opt-in, skipped when `codex` is absent):** generate the schema with the
  installed binary and assert the method names and the fields we read still exist.
- **Live acceptance on the host before release:** run the new collector on `ubuntu` beside
  production on another port, confirm the Codex card against `/status` in the Codex TUI,
  and look at the clock on a full poll.

## Open items

1. Resolved 2026-09-17 by reading Codex source: a dead login surfaces as HTTP 401 inside
   `-32603` (section 5). Not reproducible by experiment without burning a real login.
2. `--device-auth` for a second account on this plan (openai/codex issue 9253 reports a
   plan gate for some workspaces). Fallback: SSH-forward `localhost:1455`. Only matters when
   a second Codex account is added; the first one on the host is already logged in.
3. README note only, not a blocker: the `codex` binary is a 263 MB static executable, so on a
   Pi-class host reading it off an SD card the first launch may take seconds rather than the
   0.3 s measured on the production server. The 20 s deadline covers it and `CODEX_TIMEOUT`
   is configurable.

## Login model

Login is a user-space action, the same as `cswap add` today. The human runs
`codex login` (or `codex login --device-auth`) in their own shell as the Unix user the
collector runs as (`User=a` in the unit), and Codex writes the credential into that user's
home: `~/.codex` for the first account, `~/.claudetv/codex/<alias>` (mode 0700) for others.
No root, no service restart, no token ever passes through the collector, the terminal, or
`.env`. The collector only launches `codex` with `CODEX_HOME` pointed at the directory. The
optional `--codex-login <alias>` flag is a wrapper that execs that same interactive command;
it is never run by the service.
