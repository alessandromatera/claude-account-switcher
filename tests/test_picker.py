import time
import unittest
from unittest import mock

from cas import picker
from cas.config import DEFAULT_SETTINGS, Profile
from cas.usage import Effective

NOW = time.time()


def active(pct, resets_in_h, week=20.0):
    return Effective(five_pct=pct, five_resets=NOW + resets_in_h * 3600, week_pct=week, window="active")


def idle(week=20.0):
    return Effective(five_pct=0.0, week_pct=week, window="idle")


class PickerTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(Profile, "logged_in", lambda self: self.name != "nologin")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.s = dict(DEFAULT_SETTINGS)

    def names(self, items, **kw):
        ordered, blocked = picker.rank(items, self.s, **kw)
        return [c.profile.name for c in ordered], [b.profile.name for b in blocked]

    def test_open_window_resetting_soonest_goes_first(self):
        items = [(Profile("fresh"), idle()), (Profile("late"), active(30, 4)), (Profile("soon"), active(50, 1))]
        self.assertEqual(self.names(items)[0], ["soon", "late", "fresh"])

    def test_thresholds_block(self):
        items = [(Profile("full"), active(95, 1)), (Profile("weekly"), active(10, 3, week=99)),
                 (Profile("nologin"), idle()), (Profile("ok"), idle())]
        ordered, blocked = self.names(items)
        self.assertEqual(ordered, ["ok"])
        self.assertEqual(set(blocked), {"full", "weekly", "nologin"})

    def test_little_headroom_ranks_after_idle(self):
        items = [(Profile("almost"), active(80, 0.5)), (Profile("idle"), idle())]
        self.assertEqual(self.names(items)[0], ["idle", "almost"])

    def test_idle_prefers_lower_weekly(self):
        items = [(Profile("a"), idle(week=70)), (Profile("b"), idle(week=10))]
        self.assertEqual(self.names(items)[0], ["b", "a"])

    def test_exclude_and_soonest_unblock(self):
        items = [(Profile("a"), active(99, 2)), (Profile("b"), active(99, 1))]
        ordered, blocked = picker.rank(items, self.s, exclude=["x"])
        self.assertEqual(ordered, [])
        self.assertEqual(picker.soonest_unblock(blocked).profile.name, "b")


if __name__ == "__main__":
    unittest.main()
