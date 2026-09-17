#!/usr/bin/env python3
"""ClaudeTV collector tests (stdlib unittest, no deps).

    cd host && python3 -m unittest test_collector -v

Covers the multi-account seam: mapping `cswap list --json` into account records, the
backwards-compatible /usage wire contract, and per-account reset detection (including the
account-switch-is-not-a-reset case that produced a phantom 'gift' in the single-account build).
"""
import json, os, tempfile, unittest

import claude_usage_server as srv


# A trimmed but structurally faithful `cswap list --json` payload (schemaVersion 1).
CSWAP_DOC = {
    "schemaVersion": 1,
    "activeAccountNumber": 1,
    "accounts": [
        {
            "number": 1, "email": "aditya@latticelabs.au", "alias": "work",
            "organizationName": "aditya@latticelabs.au's Organization",
            "active": True, "usageStatus": "ok", "usageAgeSeconds": 81.3,
            "usage": {
                "fiveHour": {"pct": 27.0, "resetsAt": "2026-08-25T17:00:00.493975+00:00"},
                "sevenDay": {"pct": 6.0, "resetsAt": "2026-08-27T01:00:00.493995+00:00"},
                "scoped": [{"pct": 2.0, "name": "Fable",
                            "resetsAt": "2026-08-27T01:00:00.494222+00:00"}],
            },
        },
        {
            "number": 2, "email": "varma.adityaa@gmail.com", "alias": "personal",
            "organizationName": "varma.adityaa@gmail.com's Organization",
            "active": False, "usageStatus": "ok", "usageAgeSeconds": 584.0,
            "usage": {
                "fiveHour": {"pct": 100.0, "resetsAt": "2026-08-25T13:19:59.730620+00:00"},
                "sevenDay": {"pct": 73.0, "resetsAt": "2026-08-30T23:59:59.730646+00:00"},
                "scoped": [{"pct": 51.0, "name": "Fable",
                            "resetsAt": "2026-08-30T23:59:59.730866+00:00"}],
            },
        },
    ],
}


def doc(**over):
    d = json.loads(json.dumps(CSWAP_DOC))
    d.update(over)
    return d


class TzPinned(unittest.TestCase):
    """Reset strings are rendered in CONFIG['TZ']; pin it so assertions are stable."""

    def setUp(self):
        self._tz = srv.CONFIG.get("TZ")
        srv.CONFIG["TZ"] = "UTC"

    def tearDown(self):
        srv.CONFIG["TZ"] = self._tz


class TestCswapMapping(TzPinned):
    def test_maps_both_accounts_in_slot_order(self):
        accts = srv.cswap_accounts_from_json(doc())
        self.assertEqual([a["label"] for a in accts], ["WORK", "PERSONAL"])
        self.assertEqual([a["email"] for a in accts],
                         ["aditya@latticelabs.au", "varma.adityaa@gmail.com"])

    def test_percentages_and_scoped_limit(self):
        work, personal = srv.cswap_accounts_from_json(doc())
        self.assertEqual(work["u"]["s"], 27)
        self.assertEqual(work["u"]["w"], 6)
        self.assertEqual(work["u"]["f"], 2)
        self.assertEqual(work["u"]["fl"], "FABLE")
        self.assertEqual(personal["u"]["s"], 100)
        self.assertEqual(personal["u"]["f"], 51)

    def test_reset_strings_match_the_device_format(self):
        work = srv.cswap_accounts_from_json(doc())[0]
        # Same formatters the single-account build fed the ESP, so the card geometry still fits.
        self.assertEqual(work["u"]["sr"], "5:00pm")
        self.assertEqual(work["u"]["wr"], "Aug 27 1am")

    def test_resets_carry_iso_for_the_notifier(self):
        work = srv.cswap_accounts_from_json(doc())[0]
        self.assertEqual(work["resets"]["session"], "2026-08-25T17:00:00.493975+00:00")
        self.assertEqual(work["resets"]["week"], "2026-08-27T01:00:00.493995+00:00")

    def test_account_without_scoped_limit_reports_minus_one(self):
        d = doc()
        d["accounts"][0]["usage"]["scoped"] = []
        work = srv.cswap_accounts_from_json(d)[0]
        self.assertEqual(work["u"]["f"], -1)
        self.assertEqual(work["u"]["fl"], "")

    def test_key_is_stable_and_identity_bearing(self):
        a = srv.cswap_accounts_from_json(doc())[0]
        b = srv.cswap_accounts_from_json(doc())[0]
        self.assertEqual(a["key"], b["key"])
        self.assertIn("aditya@latticelabs.au", a["key"])

    def test_label_falls_back_to_email_local_part_and_is_capped(self):
        d = doc()
        del d["accounts"][0]["alias"]
        d["accounts"][0]["email"] = "averylongaddress@example.com"
        self.assertEqual(srv.cswap_accounts_from_json(d)[0]["label"], "AVERYLON")

    def test_unhealthy_account_is_marked_dead_but_still_listed(self):
        # "relogin_required" is one of cswap's real dead states (see CSWAP_DEAD); a status it
        # never emits must not be treated as dead, which is what test_transient_statuses covers.
        d = doc()
        d["accounts"][1]["usageStatus"] = "relogin_required"
        d["accounts"][1]["usage"] = None
        accts = srv.cswap_accounts_from_json(d)
        self.assertEqual(len(accts), 2)
        self.assertEqual(accts[1]["auth"], "dead")
        self.assertEqual(accts[0]["auth"], "ok")

    def test_rejects_unknown_schema_version(self):
        with self.assertRaises(ValueError):
            srv.cswap_accounts_from_json(doc(schemaVersion=2))

    def test_filter_selects_and_orders_by_config(self):
        accts = srv.cswap_accounts_from_json(doc(), only="personal")
        self.assertEqual([a["label"] for a in accts], ["PERSONAL"])

    def test_filter_matches_on_email_too(self):
        accts = srv.cswap_accounts_from_json(doc(), only="varma.adityaa@gmail.com")
        self.assertEqual([a["label"] for a in accts], ["PERSONAL"])


def many(n):
    """A cswap payload with n accounts — nothing in the collector may assume there are two."""
    d = doc()
    proto = d["accounts"][0]
    d["accounts"] = []
    for i in range(1, n + 1):
        a = json.loads(json.dumps(proto))
        a.update({"number": i, "email": "user%d@example.com" % i, "alias": "acct%d" % i,
                  "active": i == 1})
        a["usage"]["fiveHour"]["pct"] = float(i)
        d["accounts"].append(a)
    return d


class TestManyAccounts(TzPinned):
    def test_maps_every_account_not_just_two(self):
        for n in (1, 3, 5, 12):
            accts = srv.cswap_accounts_from_json(many(n))
            self.assertEqual(len(accts), n, "n=%d" % n)
            self.assertEqual([a["label"] for a in accts],
                             ["ACCT%d" % i for i in range(1, n + 1)])

    def test_wire_carries_every_account(self):
        accts = srv.cswap_accounts_from_json(many(7))
        w = srv.usage_wire(accts, {})
        self.assertEqual(w["n"], 7)
        self.assertEqual(len(w["acc"]), 7)
        self.assertEqual([a["s"] for a in w["acc"]], [1, 2, 3, 4, 5, 6, 7])

    def test_filter_picks_a_subset_in_the_requested_order(self):
        accts = srv.cswap_accounts_from_json(many(6), only="acct5, acct2 ,acct4")
        self.assertEqual([a["label"] for a in accts], ["ACCT5", "ACCT2", "ACCT4"])

    def test_filter_ignores_names_that_match_nothing(self):
        accts = srv.cswap_accounts_from_json(many(4), only="acct2,ghost")
        self.assertEqual([a["label"] for a in accts], ["ACCT2"])

    def test_filter_matches_by_slot_number(self):
        accts = srv.cswap_accounts_from_json(many(4), only="3")
        self.assertEqual([a["label"] for a in accts], ["ACCT3"])

    def test_every_account_gets_its_own_notify_namespace(self):
        accts = srv.cswap_accounts_from_json(many(5))
        self.assertEqual(len({a["key"] for a in accts}), 5)


class TestUnavailableUsage(TzPinned):
    """cswap sets usage=None for EVERY non-ok status, and usage_to_json emits fiveHour /
    sevenDay / scoped only when present. Absent data must never become 0%: the notifier reads
    a drop to 0 as a reset, which is how an account switch got logged as an Anthropic 'gift'."""

    def _one(self, **over):
        d = doc()
        d["accounts"] = [d["accounts"][1]]
        d["accounts"][0].update(over)
        return srv.cswap_accounts_from_json(d)[0]

    def test_null_usage_is_marked_stale_not_zero(self):
        rec = self._one(usageStatus="unavailable", usage=None)
        self.assertTrue(rec["stale"])
        self.assertNotEqual(rec["u"]["s"], 0)
        self.assertNotEqual(rec["u"]["w"], 0)

    def test_null_usage_falls_back_to_last_good_for_display(self):
        rec = self._one(usageStatus="foreign_credential", usage=None,
                        lastGoodUsage={"fiveHour": {"pct": 42.0,
                                                    "resetsAt": "2026-08-25T17:00:00+00:00"},
                                       "sevenDay": {"pct": 61.0,
                                                    "resetsAt": "2026-08-27T01:00:00+00:00"}})
        self.assertTrue(rec["stale"])
        self.assertEqual(rec["u"]["s"], 42)
        self.assertEqual(rec["u"]["w"], 61)

    def test_missing_seven_day_inside_ok_is_stale_not_zero(self):
        # the exact fossil found in notify_state.json: w=0 recorded beside f=56
        d = doc()
        d["accounts"] = [d["accounts"][1]]
        del d["accounts"][0]["usage"]["sevenDay"]
        rec = srv.cswap_accounts_from_json(d)[0]
        self.assertTrue(rec["stale"])
        self.assertNotEqual(rec["u"]["w"], 0)

    def test_missing_five_hour_inside_ok_is_stale(self):
        d = doc()
        d["accounts"] = [d["accounts"][1]]
        del d["accounts"][0]["usage"]["fiveHour"]
        self.assertTrue(srv.cswap_accounts_from_json(d)[0]["stale"])

    def test_complete_ok_usage_is_not_stale(self):
        self.assertFalse(self._one()["stale"])

    def test_transient_statuses_are_not_dead(self):
        for st in ("token_expired", "keychain_unavailable", "foreign_credential",
                   "unavailable", "unknown", "error"):
            rec = self._one(usageStatus=st, usage=None)
            self.assertNotEqual(rec["auth"], "dead", "%s must not read as a dead login" % st)
            self.assertTrue(rec["stale"], st)

    def test_only_real_relogin_states_are_dead(self):
        for st in ("relogin_required", "no_credentials", "expired"):
            self.assertEqual(self._one(usageStatus=st, usage=None)["auth"], "dead", st)

    def test_api_key_account_has_no_subscription_quota(self):
        rec = self._one(usageStatus="api_key", usage=None)
        self.assertNotEqual(rec["auth"], "dead")
        self.assertTrue(rec["stale"])

    def test_stale_accounts_are_never_fed_to_the_notifier(self):
        good = self._one()
        bad = self._one(usageStatus="unavailable", usage=None)
        self.assertTrue(srv.notifiable(good))
        self.assertFalse(srv.notifiable(bad))


class TestNotifyKeyStability(TzPinned):
    """cswap omits `alias` entirely when unset, so the LABEL is volatile. State must key on the
    stable account identity or one account grows two namespaces with divergent history, and the
    stale one then reads as a huge drop."""

    def test_key_survives_the_alias_being_removed(self):
        with_alias = srv.cswap_accounts_from_json(doc())[1]
        d = doc()
        del d["accounts"][1]["alias"]
        without = srv.cswap_accounts_from_json(d)[1]
        self.assertNotEqual(with_alias["label"], without["label"])   # label does change
        self.assertEqual(with_alias["key"], without["key"])          # identity does not

    def test_notify_namespace_is_the_key_not_the_label(self):
        d = doc()
        del d["accounts"][1]["alias"]
        self.assertEqual(srv.notify_key(srv.cswap_accounts_from_json(doc())[1]),
                         srv.notify_key(srv.cswap_accounts_from_json(d)[1]))


CSWAP_SETTINGS = {
    "schemaVersion": 1,
    "path": "/home/a/.local/share/claude-swap/settings.json",
    "settings": [
        {"key": "autoswitch.threshold", "value": 95.0, "isSet": True},
        {"key": "autoswitch.intervalSeconds", "value": 60.0, "isSet": False},
        {"key": "autoswitch.cooldownSeconds", "value": 300.0, "isSet": False},
        {"key": "autoswitch.hysteresisPct", "value": 5.0, "isSet": True},
        {"key": "autoswitch.strategy", "value": "consume-first", "isSet": True},
        {"key": "autoswitch.includeApiKeyAccounts", "value": False, "isSet": False},
        {"key": "autoswitch.model", "value": None, "isSet": False},
    ],
}


class TestSwitchPolicy(unittest.TestCase):
    """The thresholds that decide "are we actually blocked" belong to cswap, not to ClaudeTV.
    Read them from `cswap config --json` so a user who tuned cswap gets consistent behaviour."""

    def setUp(self):
        self._cfg = {k: srv.CONFIG.get(k) for k in
                     ("MAXED_THRESHOLD", "MAXED_SCOPE", "NOTIFY_FLEET_MAXED")}
        for k in self._cfg: srv.CONFIG[k] = ""

    def tearDown(self):
        srv.CONFIG.update({k: (v if v is not None else "") for k, v in self._cfg.items()})

    def test_reads_thresholds_from_cswap(self):
        p = srv.switch_policy_from_json(CSWAP_SETTINGS)
        self.assertEqual(p["threshold"], 95.0)
        self.assertEqual(p["hysteresis"], 5.0)
        self.assertEqual(p["cooldown"], 300.0)
        self.assertEqual(p["strategy"], "consume-first")
        self.assertIsNone(p["model"])

    def test_falls_back_to_cswap_defaults_when_unreadable(self):
        p = srv.switch_policy_from_json(None)
        self.assertEqual(p["threshold"], srv.SWITCH_DEFAULTS["threshold"])
        self.assertEqual(p["strategy"], srv.SWITCH_DEFAULTS["strategy"])

    def test_claudetv_config_overrides_cswap(self):
        srv.CONFIG["MAXED_THRESHOLD"] = "80"
        p = srv.switch_policy_from_json(CSWAP_SETTINGS)
        self.assertEqual(p["threshold"], 80.0)
        self.assertEqual(p["hysteresis"], 5.0)      # untouched keys still come from cswap

    def test_ignores_a_nonsense_override(self):
        srv.CONFIG["MAXED_THRESHOLD"] = "banana"
        self.assertEqual(srv.switch_policy_from_json(CSWAP_SETTINGS)["threshold"], 95.0)


class TestFleetExhaustion(TzPinned):
    """With auto-switch, ONE account hitting its cap is not a block: cswap moves to another and
    Claude keeps running. You are only actually blocked when no account has headroom left."""

    POLICY = {"threshold": 95.0, "hysteresis": 5.0, "cooldown": 300.0,
              "strategy": "consume-first", "model": None}

    def acct(self, label, s, w, f=-1, stale=False, disabled=False, auth="ok"):
        return {"key": "k:" + label, "label": label.upper(), "email": label + "@e.com",
                "active": False, "stale": stale, "disabled": disabled, "auth": auth,
                "age": 5, "err": "", "resets": {"session": None, "week": None},
                "u": {"s": s, "w": w, "f": f, "fl": "FABLE" if f >= 0 else "",
                      "sr": "", "wr": ""}}

    def test_binding_window_is_the_worse_of_session_and_week(self):
        self.assertEqual(srv.binding_pct(self.acct("a", 20, 80), self.POLICY), 80)
        self.assertEqual(srv.binding_pct(self.acct("a", 96, 10), self.POLICY), 96)

    def test_scoped_window_counts_only_when_cswap_tracks_a_model(self):
        rec = self.acct("a", 10, 10, f=99)
        self.assertEqual(srv.binding_pct(rec, self.POLICY), 10)
        p = dict(self.POLICY, model="Fable")
        self.assertEqual(srv.binding_pct(rec, p), 99)

    def test_one_maxed_account_is_not_a_block(self):
        fleet = [self.acct("work", 99, 40), self.acct("personal", 5, 20)]
        st = srv.fleet_state(fleet, self.POLICY)
        self.assertFalse(st["exhausted"])
        self.assertEqual(st["headroom"], ["PERSONAL"])

    def test_every_account_maxed_is_a_block(self):
        fleet = [self.acct("work", 99, 40), self.acct("personal", 20, 97)]
        st = srv.fleet_state(fleet, self.POLICY)
        self.assertTrue(st["exhausted"])
        self.assertEqual(st["headroom"], [])

    def test_exactly_at_the_threshold_counts_as_exhausted(self):
        st = srv.fleet_state([self.acct("a", 95, 0)], self.POLICY)
        self.assertTrue(st["exhausted"])

    def test_stale_accounts_cannot_prove_exhaustion(self):
        # unknown usage is not evidence of being out; refuse to claim a block we cannot see
        fleet = [self.acct("work", 99, 40), self.acct("personal", -1, -1, stale=True)]
        self.assertFalse(srv.fleet_state(fleet, self.POLICY)["exhausted"])

    def test_disabled_accounts_are_excluded_from_the_fleet(self):
        # cswap will never switch onto a disabled account, so its headroom is not available
        fleet = [self.acct("work", 99, 40), self.acct("spare", 1, 1, disabled=True)]
        self.assertTrue(srv.fleet_state(fleet, self.POLICY)["exhausted"])

    def test_a_benched_account_with_room_is_named_as_the_reason(self):
        # the live case: work capped, the spare sitting at 7% but held out by `cswap disable`.
        # Excluding it from the verdict is right; failing to SAY so is what read as a broken
        # detector, since "every account is out of quota" is false of the account you can see.
        fleet = [self.acct("work", 100, 40), self.acct("personal", 7, 42, disabled=True)]
        st = srv.fleet_state(fleet, self.POLICY)
        self.assertTrue(st["exhausted"])
        self.assertEqual(st["benched"], [{"label": "PERSONAL", "pct": 42, "why": "disabled"}])

    def test_a_dead_account_is_benched_with_its_own_reason(self):
        fleet = [self.acct("work", 99, 40), self.acct("old", 1, 1, auth="dead")]
        st = srv.fleet_state(fleet, self.POLICY)
        self.assertEqual([b["why"] for b in st["benched"]], ["login expired"])

    def test_a_benched_account_without_room_is_not_offered_as_a_way_out(self):
        # enabling it would not help: it is over the threshold too
        fleet = [self.acct("work", 99, 40), self.acct("spare", 98, 20, disabled=True)]
        self.assertEqual(srv.fleet_state(fleet, self.POLICY)["benched"], [])

    def test_a_benched_account_we_cannot_read_is_not_offered_either(self):
        fleet = [self.acct("work", 99, 40), self.acct("spare", -1, -1, stale=True, disabled=True)]
        st = srv.fleet_state(fleet, self.POLICY)
        self.assertEqual(st["benched"], [])
        self.assertTrue(st["exhausted"])      # unreadable AND benched: still not a candidate

    def test_benched_accounts_do_not_count_as_unreadable(self):
        # a disabled account must not suppress the verdict the way a stale in-rotation one does
        fleet = [self.acct("work", 99, 40), self.acct("spare", 1, 1, disabled=True)]
        self.assertEqual(srv.fleet_state(fleet, self.POLICY)["unknown"], 0)

    def test_dead_accounts_are_excluded_from_the_fleet(self):
        fleet = [self.acct("work", 99, 40), self.acct("old", 1, 1, auth="dead")]
        self.assertTrue(srv.fleet_state(fleet, self.POLICY)["exhausted"])

    def test_no_usable_accounts_is_not_reported_as_exhausted(self):
        self.assertFalse(srv.fleet_state([self.acct("a", -1, -1, stale=True)],
                                         self.POLICY)["exhausted"])

    def test_binding_account_is_named(self):
        fleet = [self.acct("work", 99, 40), self.acct("personal", 5, 20)]
        self.assertEqual(srv.fleet_state(fleet, self.POLICY)["best"], "PERSONAL")


class TestFleetAlerts(TzPinned):
    POLICY = TestFleetExhaustion.POLICY

    def setUp(self):
        super().setUp()
        self.sent = []; self.bodies = []
        self._alert = srv._fleet_alert
        srv._fleet_alert = lambda ev, body: (self.sent.append(ev), self.bodies.append(body))
        self._state = dict(srv._fleet_last)
        srv._fleet_last.clear()

    def tearDown(self):
        srv._fleet_alert = self._alert
        srv._fleet_last.clear(); srv._fleet_last.update(self._state)
        super().tearDown()

    def acct(self, *a, **k): return TestFleetExhaustion.acct(self, *a, **k)

    def test_fires_once_when_the_fleet_runs_out(self):
        out = [self.acct("work", 99, 40), self.acct("personal", 97, 20)]
        srv.fleet_check(out, self.POLICY)
        srv.fleet_check(out, self.POLICY)
        self.assertEqual(self.sent, ["exhausted"])

    def test_does_not_fire_while_any_account_has_room(self):
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 5, 5)], self.POLICY)
        self.assertEqual(self.sent, [])

    def test_recovery_fires_once_after_a_block(self):
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 97, 20)], self.POLICY)
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 2, 20)], self.POLICY)
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 3, 20)], self.POLICY)
        self.assertEqual(self.sent, ["exhausted", "recovered"])

    def test_a_block_caused_by_a_benched_account_says_so(self):
        srv.fleet_check([self.acct("work", 100, 40),
                         self.acct("personal", 7, 42, disabled=True)], self.POLICY)
        self.assertEqual(self.sent, ["benched"])
        self.assertIn("PERSONAL 42% (disabled)", self.bodies[0])
        self.assertIn("cswap enable", self.bodies[0])

    def test_a_genuine_all_out_block_keeps_the_original_wording(self):
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 97, 20)], self.POLICY)
        self.assertEqual(self.sent, ["exhausted"])
        self.assertIn("Every Claude account", self.bodies[0])

    def test_a_switch_between_two_healthy_accounts_is_silent(self):
        a = [self.acct("work", 96, 40), self.acct("personal", 5, 20)]
        b = [self.acct("work", 96, 40), self.acct("personal", 30, 20)]
        srv.fleet_check(a, self.POLICY); srv.fleet_check(b, self.POLICY)
        self.assertEqual(self.sent, [])

    def test_a_stale_poll_does_not_clear_an_active_block(self):
        out = [self.acct("work", 99, 40), self.acct("personal", 97, 20)]
        srv.fleet_check(out, self.POLICY)
        srv.fleet_check([self.acct("work", -1, -1, stale=True),
                         self.acct("personal", -1, -1, stale=True)], self.POLICY)
        self.assertEqual(self.sent, ["exhausted"])   # no phantom "recovered"

    def test_a_partly_readable_poll_does_not_clear_an_active_block(self):
        """The nastier half of the same bug: with one account still visibly out and the other
        unreadable, `exhausted` correctly goes false — but that is "cannot tell", not "you have
        room", and must not sound the all-clear."""
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 97, 20)], self.POLICY)
        srv.fleet_check([self.acct("work", 99, 40),
                         self.acct("personal", -1, -1, stale=True)], self.POLICY)
        self.assertEqual(self.sent, ["exhausted"])

    def test_recovery_needs_an_account_with_actual_headroom(self):
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 97, 20)], self.POLICY)
        srv.fleet_check([self.acct("work", 99, 40),
                         self.acct("personal", -1, -1, stale=True)], self.POLICY)
        srv.fleet_check([self.acct("work", 99, 40), self.acct("personal", 10, 20)], self.POLICY)
        self.assertEqual(self.sent, ["exhausted", "recovered"])


RELEASE = {
    "tag_name": "v5.1",
    "published_at": "2026-08-27T10:00:00Z",
    "body": "### What's new\nStuff.",
    "assets": [
        {"name": "claudetv-v5.1-generic.bin",
         "browser_download_url": "https://github.com/x/releases/download/v5.1/claudetv-v5.1-generic.bin",
         "size": 505968},
        {"name": "checksums.txt", "browser_download_url": "https://x/checksums.txt", "size": 90},
    ],
}


class TestFirmwareUpdater(unittest.TestCase):
    """Flashing should not require a laptop, a download and a curl incantation: the collector
    already talks to both GitHub and the device's OTA endpoint."""

    def test_picks_the_generic_image_out_of_the_release(self):
        r = srv.parse_release(RELEASE)
        self.assertEqual(r["tag"], "v5.1")
        self.assertEqual(r["name"], "claudetv-v5.1-generic.bin")
        self.assertTrue(r["url"].endswith("claudetv-v5.1-generic.bin"))
        self.assertEqual(r["size"], 505968)

    def test_release_without_a_firmware_asset_is_not_offered(self):
        r = srv.parse_release({"tag_name": "v9", "assets": [{"name": "notes.txt"}]})
        self.assertEqual(r["url"], "")

    def test_version_comparison_ignores_the_v_prefix(self):
        self.assertTrue(srv.update_available("5.0", "v5.1"))
        self.assertTrue(srv.update_available("v5.0", "5.0.1"))
        self.assertFalse(srv.update_available("5.1", "v5.1"))
        self.assertFalse(srv.update_available("v5.2", "v5.1"))

    def test_unknown_current_version_does_not_claim_an_update(self):
        # a device we cannot reach must not be reported as out of date
        self.assertFalse(srv.update_available("", ""))

    def test_multipart_body_matches_what_the_esp_update_endpoint_expects(self):
        ctype, body = srv._multipart("firmware", "fw.bin", b"\xde\xad\xbe\xef")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=")[1]
        self.assertIn(b'name="firmware"', body)
        self.assertIn(b'filename="fw.bin"', body)
        self.assertIn(b"\xde\xad\xbe\xef", body)
        self.assertTrue(body.rstrip().endswith(("--%s--" % boundary).encode()))

    def test_refuses_to_flash_something_that_is_not_a_firmware_image(self):
        with self.assertRaises(ValueError):
            srv.check_image(b"<!DOCTYPE html><html>nope</html>")

    def test_accepts_a_plausible_esp8266_image(self):
        srv.check_image(b"\xe9" + b"\x00" * 200000)      # ESP magic byte, sane size


class TestEmbeddedJavaScript(unittest.TestCase):
    """The dashboards are JS embedded in a Python triple-quoted string, so a `\\n` meant for
    JavaScript silently becomes a REAL newline and splits a string literal across lines — which
    kills the whole script and leaves a blank dashboard with only a console error. That shipped
    once; these are the cheap structural checks that catch it without a JS engine."""

    def _script(self, text):
        self.assertIn("<script>", text)
        return text.split("<script>", 1)[1].split("</script>", 1)[0]

    def _assert_sane(self, js, where):
        for n, line in enumerate(js.splitlines(), 1):
            stripped = line.split("//")[0] if not line.strip().startswith("http") else line
            # a line ending mid-string is the exact failure mode we are guarding against
            self.assertEqual(stripped.count("'") % 2, 0,
                             "%s line %d has an unbalanced single quote: %s" % (where, n, line[:120]))
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(js.count(opener), js.count(closer),
                             "%s has unbalanced %s%s" % (where, opener, closer))

    def test_master_terminal_script_is_structurally_sound(self):
        self._assert_sane(self._script(srv.TERMINAL), "TERMINAL")

    def test_master_terminal_has_no_raw_newline_inside_a_dialog_string(self):
        # the specific bug: confirm('...\n...') written with a real newline
        for call in ("confirm(", "alert(", "prompt("):
            for chunk in srv.TERMINAL.split(call)[1:]:
                head = chunk.split(")", 1)[0]
                self.assertNotIn("\n", head, "%s... contains a real newline; use a \\\\n escape" % call)

    def test_device_panel_script_is_structurally_sound(self):
        panel = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "firmware", "claudetv", "panel.h")
        if not os.path.exists(panel):
            self.skipTest("panel.h not present")
        with open(panel, encoding="utf-8") as f:
            self._assert_sane(self._script(f.read()), "panel.h")


class TestWireContract(TzPinned):
    def setUp(self):
        super().setUp()
        self.accts = srv.cswap_accounts_from_json(doc())
        self.wx = {"city": "Melbourne", "wc": "Drizzle", "wt": 12}

    def test_flat_keys_mirror_the_first_account(self):
        w = srv.usage_wire(self.accts, self.wx)
        # A v4.7 device reads only these; it must keep working untouched.
        for k in ("s", "w", "sr", "wr", "f", "fl"):
            self.assertEqual(w[k], self.accts[0]["u"][k], k)

    def test_exposes_the_account_array_and_count(self):
        w = srv.usage_wire(self.accts, self.wx)
        self.assertEqual(w["n"], 2)
        self.assertEqual([a["l"] for a in w["acc"]], ["WORK", "PERSONAL"])
        self.assertEqual(w["acc"][1]["s"], 100)

    def test_weather_is_merged_once_not_per_account(self):
        w = srv.usage_wire(self.accts, self.wx)
        self.assertEqual(w["city"], "Melbourne")
        self.assertNotIn("city", w["acc"][0])

    def test_primary_selects_which_account_fills_flat_keys(self):
        w = srv.usage_wire(self.accts, self.wx, primary="personal")
        self.assertEqual(w["s"], 100)
        self.assertEqual(w["acc"][0]["l"], "PERSONAL")   # requested account leads the array

    def test_unknown_primary_falls_back_to_first_account(self):
        w = srv.usage_wire(self.accts, self.wx, primary="nope")
        self.assertEqual(w["s"], 27)

    def test_no_accounts_reports_not_ok(self):
        w = srv.usage_wire([], self.wx)
        self.assertEqual(w["ok"], 0)
        self.assertEqual(w["n"], 0)
        self.assertEqual(w["s"], 0)

    def test_top_level_auth_is_dead_only_when_every_account_is_dead(self):
        one_dead = [dict(self.accts[0], auth="dead"), self.accts[1]]
        self.assertEqual(srv.usage_wire(one_dead, self.wx)["auth"], "ok")
        all_dead = [dict(a, auth="dead") for a in self.accts]
        self.assertEqual(srv.usage_wire(all_dead, self.wx)["auth"], "dead")

    def test_per_account_auth_travels_in_the_array(self):
        accts = [dict(self.accts[0], auth="dead"), self.accts[1]]
        w = srv.usage_wire(accts, self.wx)
        self.assertEqual(w["acc"][0]["auth"], "dead")
        self.assertEqual(w["acc"][1]["auth"], "ok")


class TestPerAccountResets(TzPinned):
    """notify_check keys its state per account, so two accounts cannot clobber each other and a
    credential swap cannot masquerade as a reset."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self._paths = (srv.NOTIFY_STATE_PATH, srv.RESET_LOG_PATH)
        srv.NOTIFY_STATE_PATH = os.path.join(self.tmp.name, "notify_state.json")
        srv.RESET_LOG_PATH = os.path.join(self.tmp.name, "resets.log")
        srv._notify_state = None
        srv._reset_log = None
        self._notify_cfg = {k: srv.CONFIG.get(k) for k in
                            ("NOTIFY_DISCORD_WEBHOOK", "NOTIFY_SLACK_WEBHOOK", "NOTIFY_EMAIL")}
        for k in self._notify_cfg:                       # no channels configured -> nothing sends
            srv.CONFIG[k] = ""

    def tearDown(self):
        srv.NOTIFY_STATE_PATH, srv.RESET_LOG_PATH = self._paths
        srv._notify_state = None
        srv._reset_log = None
        srv.CONFIG.update(self._notify_cfg)
        self.tmp.cleanup()
        super().tearDown()

    def _check(self, acct, s, w, session_ra, week_ra):
        srv.notify_check({"s": s, "w": w, "f": -1, "fl": ""},
                         {"session": session_ra, "week": week_ra}, acct=acct)

    def test_first_sight_baselines_silently(self):
        self._check("work", 27, 6, "2026-08-25T17:00:00+00:00", "2026-08-27T01:00:00+00:00")
        self.assertEqual(srv._load_reset_log(), [])

    def test_state_is_namespaced_per_account(self):
        self._check("work", 27, 6, "2026-08-25T17:00:00+00:00", "2026-08-27T01:00:00+00:00")
        self._check("personal", 100, 73, "2026-08-25T13:19:00+00:00", "2026-08-30T23:59:00+00:00")
        st = srv._load_notify_state()
        self.assertIn("work", st)
        self.assertIn("personal", st)
        self.assertEqual(st["work"]["session"]["s"], 27)
        self.assertEqual(st["personal"]["session"]["s"], 100)

    def test_second_account_high_usage_does_not_read_as_a_reset_of_the_first(self):
        # The single-account build logged exactly this as a phantom 'gift' when the credential
        # file behind it changed account: 73% -> 0% is a drop only if you conflate the accounts.
        self._check("work", 73, 73, "2026-08-25T17:00:00+00:00", "2026-08-27T01:00:00+00:00")
        self._check("personal", 0, 0, "2026-08-25T13:19:00+00:00", "2026-08-30T23:59:00+00:00")
        self.assertEqual(srv._load_reset_log(), [])

    def test_a_real_drop_within_one_account_still_fires(self):
        self._check("work", 73, 73, "2026-08-25T17:00:00+00:00", "2026-08-27T01:00:00+00:00")
        self._check("work", 0, 0, "2026-08-25T17:00:00+00:00", "2026-08-27T01:00:00+00:00")
        logged = srv._load_reset_log()
        self.assertTrue(logged)
        self.assertEqual({e["window"] for e in logged}, {"session", "week"})

    def test_a_cswap_switch_does_not_log_a_gift_reset(self):
        """Regression, reported 2026-08-27: a routine `cswap auto` switch alerted as an
        Anthropic gift. Mid-switch cswap returns usageStatus=foreign_credential with usage=None;
        the mapper used to coerce that to 0% and the notifier read 86% -> 0% as a reset."""
        d = doc()
        d["accounts"] = [d["accounts"][1]]
        d["accounts"][0]["usage"]["sevenDay"]["pct"] = 86.0
        live = srv.cswap_accounts_from_json(d)[0]
        srv.notify_check(live["u"], live["resets"], acct=srv.notify_key(live))
        self.assertEqual(srv._load_reset_log(), [])          # baseline, silent

        d["accounts"][0].update(usageStatus="foreign_credential", usage=None)
        mid = srv.cswap_accounts_from_json(d)[0]
        self.assertFalse(srv.notifiable(mid))                # the guard that fixes it
        if srv.notifiable(mid):
            srv.notify_check(mid["u"], mid["resets"], acct=srv.notify_key(mid))
        self.assertEqual(srv._load_reset_log(), [], "a cswap switch must not log a reset")
        self.assertNotEqual(mid["auth"], "dead", "a switch must not read as a dead login")

    def test_reset_log_entries_name_the_account(self):
        self._check("personal", 73, 73, "2026-08-25T13:19:00+00:00", "2026-08-30T23:59:00+00:00")
        self._check("personal", 0, 0, "2026-08-25T13:19:00+00:00", "2026-08-30T23:59:00+00:00")
        self.assertTrue(all(e["acct"] == "personal" for e in srv._load_reset_log()))

    def test_legacy_flat_state_migrates_onto_the_primary_account(self):
        with open(srv.NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"session": {"s": 73, "ra": "2026-08-25T17:00:00+00:00"},
                       "week": {"w": 73, "ra": "2026-08-27T01:00:00+00:00"}}, f)
        srv._notify_state = None
        accts = srv.cswap_accounts_from_json(doc())
        srv.migrate_notify_state(accts)
        st = srv._load_notify_state()
        key = srv.notify_key(accts[0])
        self.assertEqual(st[key]["session"]["s"], 73)
        self.assertNotIn("session", st)

    def test_label_keyed_state_migrates_onto_the_stable_key(self):
        accts = srv.cswap_accounts_from_json(doc())
        with open(srv.NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"personal": {"session": {"s": 5, "ra": "2026-08-26T15:49:59+00:00"}}}, f)
        srv._notify_state = None
        srv.migrate_notify_state(accts)
        st = srv._load_notify_state()
        self.assertNotIn("personal", st)
        self.assertEqual(st[srv.notify_key(accts[1])]["session"]["s"], 5)

    def test_split_namespaces_merge_keeping_the_freshest_baseline(self):
        # the real artefact: one account split across 'personal' and the email-derived 'varma.ad'
        accts = srv.cswap_accounts_from_json(doc())
        with open(srv.NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "personal": {"week": {"w": 0, "f": 56, "ra": "2026-08-30T23:59:59+00:00"}},
                "varma.ad": {"week": {"w": 90, "f": 56, "ra": "2026-09-06T23:59:59+00:00"}},
            }, f)
        srv._notify_state = None
        srv.migrate_notify_state(accts)
        st = srv._load_notify_state()
        self.assertNotIn("personal", st)
        self.assertNotIn("varma.ad", st)
        self.assertEqual(st[srv.notify_key(accts[1])]["week"]["w"], 90)   # later ra wins

    def test_migration_is_idempotent(self):
        accts = srv.cswap_accounts_from_json(doc())
        with open(srv.NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"personal": {"session": {"s": 5, "ra": "2026-08-26T15:49:59+00:00"}}}, f)
        srv._notify_state = None
        srv.migrate_notify_state(accts)
        first = json.loads(json.dumps(srv._load_notify_state()))
        self.assertFalse(srv.migrate_notify_state(accts))
        self.assertEqual(srv._load_notify_state(), first)


class TestCswapIsRequired(TzPinned):
    """cswap is the single source of accounts, for one account or twelve. There is no second
    code path: ClaudeTV does no OAuth of its own at runtime, so there is nothing to drift."""

    def test_a_single_account_goes_through_cswap_like_any_other(self):
        d = doc(); d["accounts"] = [d["accounts"][0]]
        accts = srv.cswap_accounts_from_json(d)
        self.assertEqual(len(accts), 1)
        self.assertEqual(accts[0]["label"], "WORK")
        self.assertFalse(accts[0]["stale"])

    def test_no_accounts_yields_a_setup_state_not_a_crash(self):
        w = srv.usage_wire([], {})
        self.assertEqual(w["ok"], 0)
        self.assertEqual(w["n"], 0)
        self.assertTrue(w["err"])

    def test_the_native_oauth_runtime_is_gone(self):
        # these existed only to poll Anthropic directly; cswap owns that now
        for gone in ("keeper", "refresh_token", "cred_stores", "cred_path", "fetch_usage",
                     "native_accounts", "token_status", "auth_state", "pick_source"):
            self.assertFalse(hasattr(srv, gone), "%s should have been removed" % gone)

    def test_login_survives_as_an_enrollment_helper(self):
        # a headless box has no Claude Code to log in with, so ClaudeTV still mints the
        # credential that `cswap add` then adopts
        self.assertTrue(hasattr(srv, "oauth_login"))


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

if __name__ == "__main__":
    unittest.main()
