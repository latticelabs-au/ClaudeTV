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
        d = doc()
        d["accounts"][1]["usageStatus"] = "quarantined"
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

    def test_reset_log_entries_name_the_account(self):
        self._check("personal", 73, 73, "2026-08-25T13:19:00+00:00", "2026-08-30T23:59:00+00:00")
        self._check("personal", 0, 0, "2026-08-25T13:19:00+00:00", "2026-08-30T23:59:00+00:00")
        self.assertTrue(all(e["acct"] == "personal" for e in srv._load_reset_log()))

    def test_legacy_flat_state_migrates_onto_the_primary_account(self):
        with open(srv.NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"session": {"s": 73, "ra": "2026-08-25T17:00:00+00:00"},
                       "week": {"w": 73, "ra": "2026-08-27T01:00:00+00:00"}}, f)
        srv._notify_state = None
        srv.migrate_notify_state("work")
        st = srv._load_notify_state()
        self.assertEqual(st["work"]["session"]["s"], 73)
        self.assertNotIn("session", st)


class TestBackendSelection(unittest.TestCase):
    def test_cswap_source_wins_when_it_returns_accounts(self):
        self.assertEqual(srv.pick_source(cswap_ok=True), "cswap")

    def test_falls_back_to_native_without_cswap(self):
        self.assertEqual(srv.pick_source(cswap_ok=False), "native")


if __name__ == "__main__":
    unittest.main()
