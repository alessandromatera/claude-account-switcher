import time
import unittest

from cas import resets, usage
from cas.config import Config, keychain_service_for
from tests.helpers import IsolatedHome

# Shape of a real cachedUsageUtilization written by Claude Code 2.1.330 (trimmed).
CLAUDE_CACHE = {
    "fetchedAtMs": None,  # filled per test
    "accountUuid": "acct-hub",
    "utilization": {
        "five_hour": {"utilization": 9, "resets_at": "2026-11-22T18:20:00.353410+00:00"},
        "seven_day": {"utilization": 24, "resets_at": "2026-11-27T21:00:00.353435+00:00"},
        "cedar_ember": None,
        "extra_usage": {"is_enabled": False, "monthly_limit": 10000, "used_credits": 0, "currency": "USD",
                        "decimal_places": 2, "disabled_reason": "out_of_credits"},
    },
}

CEDAR = {
    "eligible": True,
    "at_limit": False,
    "grants": [
        {"id": "g1", "label": "", "resets_total": 2, "resets_left": 1, "ends_at": "2026-12-03T00:00:00Z",
         "clears": ["five_hour"], "usable_now": False, "use_requires_limit": True, "type": "session"},
        {"id": "g2", "label": "", "resets_total": 1, "resets_left": 1, "ends_at": "2026-12-10T00:00:00Z",
         "clears": ["five_hour", "seven_day"], "type": "full"},
        {"id": "old", "resets_left": 3, "ends_at": "2020-01-01T00:00:00Z", "clears": ["five_hour"]},
        {"id": "BAD ID!", "resets_left": "x"},
    ],
    "next_grant_id": "g1",
}


class ParseTest(unittest.TestCase):
    def test_parse_time_variants(self):
        iso = usage.parse_time("2026-11-22T18:20:00.353410+00:00")
        self.assertAlmostEqual(iso, usage.parse_time("2026-11-22T18:20:00.3534Z"), places=2)
        self.assertEqual(usage.parse_time(1795354441487), 1795354441.487)
        self.assertEqual(usage.parse_time(1795354441), 1795354441.0)
        self.assertIsNone(usage.parse_time("garbage"))

    def test_effective_treats_past_reset_as_fresh(self):
        now = time.time()
        snap = usage.Snapshot(now - 7 * 3600, "claude", usage.Window(95, now - 3600), usage.Window(50, now + 86400))
        e = usage.effective(snap, now)
        self.assertEqual((e.five_pct, e.window, e.estimated), (0.0, "idle", True))
        self.assertEqual(e.week_pct, 50)

    def test_extra_usage(self):
        x = usage.parse_extra_usage(CLAUDE_CACHE["utilization"]["extra_usage"])
        self.assertEqual(usage.extra_summary(x), "off (out of credits)")
        x["enabled"], x["used"] = True, 12.5
        self.assertEqual(usage.extra_summary(x), "$87.50 of $100.00 left")

    def test_keychain_service_names(self):
        self.assertEqual(keychain_service_for(None), "Claude Code-credentials")
        self.assertRegex(keychain_service_for("/Users/x/.claude-accounts/profiles/work"),
                         r"^Claude Code-credentials-[0-9a-f]{8}$")


class ResetsTest(unittest.TestCase):
    def test_parse_and_summarize(self):
        now = usage.parse_time("2026-11-22T12:00:00Z")
        st = resets.parse_status(CEDAR, now, "live")
        self.assertEqual([g.id for g in st.grants], ["g1", "g2", "old"])
        self.assertEqual(st.count(now), 2)  # "old" expired
        self.assertEqual(st.usable(now)[0].kind, "session")
        self.assertIn("1 5-hour reset", st.summary(now))
        self.assertIn("1 full reset", st.summary(now))
        self.assertIn("use by Dec 3", st.summary(now))
        self.assertEqual(resets.limit_reset_arg(st.usable(now)[0]), ["/limit-reset 5-hour"])
        self.assertEqual(resets.limit_reset_arg(None), ["/limit-reset"])

    def test_ineligible(self):
        st = resets.parse_status({"eligible": False, "ineligible_reason": "tier"})
        self.assertFalse(st.has_any())
        self.assertIn("tier", st.summary())
        self.assertIsNone(resets.parse_status(None))


class SourcesTest(IsolatedHome):
    def test_claude_cache_and_state_freshest_wins(self):
        cfg = Config.load()
        hub = cfg.hub()
        data = self.read_json(self.hub_json)
        cache = dict(CLAUDE_CACHE, fetchedAtMs=int((time.time() - 600) * 1000))
        data["cachedUsageUtilization"] = cache
        data["unusedLimitResetStatusCache"] = {"accountUuid": "acct-hub", "holdsUnusedReset": True,
                                               "checkedAt": int(time.time() * 1000),
                                               "validUntil": int((time.time() + 3600) * 1000)}
        self.write_json(self.hub_json, data)
        s = usage.best_snapshot(hub)
        self.assertEqual((s.source, s.five_hour.pct), ("claude", 9))
        self.assertEqual(s.extra["disabled_reason"], "out_of_credits")

        usage.record_snapshot(hub.name, usage.snapshot_from_api(
            {"five_hour": {"used_percentage": 40, "resets_at": int(time.time()) + 3600}}, "statusline"))
        s = usage.best_snapshot(hub)
        self.assertEqual((s.source, s.five_hour.pct), ("statusline", 40))
        self.assertIsNotNone(s.extra, "credits carried over from the previous snapshot")

        rs = resets.best_status(hub)
        self.assertEqual((rs.source, rs.has_any(), rs.count()), ("claude", True, None))
        self.assertEqual(rs.summary(short=True), "yes")

    def test_surface_answer_is_unknown_not_none(self):
        """Real answer from the server: it won't tell cas, which says nothing about the account."""
        hub = Config.load().hub()
        resets.record(hub.name, resets.parse_status({"eligible": False, "ineligible_reason": "surface"}))
        rs = resets.best_status(hub)
        self.assertEqual((rs.summary(short=True), rs.count(), rs.maybe()), ("?", None, True))
        self.assertIn("claude.ai", rs.summary())

        data = self.read_json(self.hub_json)
        data["cachedGrowthBookFeatures"] = {"tengu_starry_waterfall": {"enabled": True}}
        self.write_json(self.hub_json, data)
        rs = resets.best_status(hub)
        # regression: the Claude Code flag being off does NOT mean "no resets" (claude.ai showed 4)
        self.assertEqual((rs.summary(short=True), rs.count(), rs.maybe(), rs.via_claude_code), ("?", None, True, False))

        data["cachedGrowthBookFeatures"]["tengu_cedar_ember"] = {"enabled": True}
        self.write_json(self.hub_json, data)
        self.assertTrue(resets.best_status(hub).via_claude_code)

    def test_manual_record_and_automatic_decrement(self):
        hub = Config.load().hub()
        resets.set_manual(hub, 4, "full", time.time() + 18 * 86400)
        rs = resets.best_status(hub)
        self.assertEqual(rs.count(), 4)
        self.assertIn("4 full resets", rs.summary())
        self.assertIn("as you recorded them", rs.summary())
        t = time.time()
        # usage drops sharply while the window is still open: a reset was used
        usage.record_snapshot(hub.name, usage.Snapshot(t + 60, "statusline", usage.Window(100, t + 7200),
                                                        usage.Window(94, t + 86400), "acct-hub"))
        usage.record_snapshot(hub.name, usage.Snapshot(t + 120, "live", usage.Window(0, t + 5 * 3600),
                                                        usage.Window(0, t + 7 * 86400), "acct-hub"))
        self.assertEqual(resets.best_status(hub).count(), 3)
        # a normal window rollover is not a reset
        usage.record_snapshot(hub.name, usage.Snapshot(t + 6 * 3600, "live", usage.Window(2, t + 11 * 3600),
                                                        usage.Window(1, t + 7 * 86400), "acct-hub"))
        self.assertEqual(resets.best_status(hub).count(), 3)
        resets.set_manual(hub, 0)
        self.assertEqual(resets.best_status(hub).count(), 0)

    def test_one_reset_with_flip_flopping_readings_counts_once(self):
        """Regression: right after a reset the status line kept repeating pre-reset numbers between
        fresh live readings, and every old->new flip was counted as another reset (4 became 1)."""
        hub = Config.load().hub()
        resets.set_manual(hub, 4, "full")
        t = time.time()
        old_w, new_w, week = t + 5 * 3600, t + 6 * 3600, t + 4 * 86400

        def rec(dt, src, pct5, r5, pct7):
            usage.record_snapshot(hub.name, usage.Snapshot(t + dt, src, usage.Window(pct5, r5),
                                                            usage.Window(pct7, week), "acct-hub"))
        rec(10, "statusline", 56, old_w, 96)   # before the reset
        rec(28, "live", 4, new_w, 1)            # reset
        rec(39, "statusline", 56, old_w, 96)   # stale repeat
        rec(40, "live", 4, new_w, 1)
        rec(44, "statusline", 56, old_w, 96)   # stale repeat
        rec(46, "live", 4, new_w, 1)
        self.assertEqual(resets.best_status(hub).count(), 3)
        self.assertEqual(usage.best_snapshot(hub).seven_day.pct, 1, "stale readings are not recorded")

    def test_weekly_only_reset_with_same_window_counts_once(self):
        hub = Config.load().hub()
        resets.set_manual(hub, 2, "weekly")
        t = time.time()
        w5, week = t + 3 * 3600, t + 4 * 86400
        for dt, pct7 in ((10, 97), (20, 3), (30, 97), (40, 3)):
            usage.record_snapshot(hub.name, usage.Snapshot(t + dt, "live", usage.Window(20, w5),
                                                            usage.Window(pct7, week), "acct-hub"))
        self.assertEqual(resets.best_status(hub).count(), 1)

    def test_cache_from_other_account_is_ignored(self):
        data = self.read_json(self.hub_json)
        data["cachedUsageUtilization"] = dict(CLAUDE_CACHE, fetchedAtMs=1, accountUuid="someone-else")
        self.write_json(self.hub_json, data)
        self.assertIsNone(usage.best_snapshot(Config.load().hub()))


if __name__ == "__main__":
    unittest.main()
