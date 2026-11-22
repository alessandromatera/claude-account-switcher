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
        now = time.time()
        resets.set_manual(hub.name, 4, "full", now + 18 * 86400)
        rs = resets.best_status(hub)
        self.assertEqual(rs.count(), 4)
        self.assertIn("4 full resets", rs.summary())
        self.assertIn("as you recorded them", rs.summary())
        # usage drops sharply while the window is still open: a reset was used
        usage.record_snapshot(hub.name, usage.Snapshot(now - 60, "statusline", usage.Window(100, now + 7200),
                                                        usage.Window(94, now + 86400), "acct-hub"))
        usage.record_snapshot(hub.name, usage.Snapshot(now, "live", usage.Window(0, now + 5 * 3600),
                                                        usage.Window(0, now + 7 * 86400), "acct-hub"))
        self.assertEqual(resets.best_status(hub).count(), 3)
        # a normal window rollover is not a reset
        usage.record_snapshot(hub.name, usage.Snapshot(now + 6 * 3600, "live", usage.Window(2, now + 11 * 3600),
                                                        usage.Window(1, now + 7 * 86400), "acct-hub"))
        self.assertEqual(resets.best_status(hub).count(), 3)
        resets.set_manual(hub.name, 0)
        self.assertIsNone(resets.manual_status(hub.name))

    def test_cache_from_other_account_is_ignored(self):
        data = self.read_json(self.hub_json)
        data["cachedUsageUtilization"] = dict(CLAUDE_CACHE, fetchedAtMs=1, accountUuid="someone-else")
        self.write_json(self.hub_json, data)
        self.assertIsNone(usage.best_snapshot(Config.load().hub()))


if __name__ == "__main__":
    unittest.main()
