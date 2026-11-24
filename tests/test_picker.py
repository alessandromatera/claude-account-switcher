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

    def test_same_minute_resets_tie_and_a_tie_keeps_the_current_account(self):
        """Regression: two windows both resetting at 14:00 were ordered by fractions of a second
        (13:59:59.81 vs 14:00:00.00), so `cas run` flipped between accounts on reading noise."""
        at = (int(NOW // 3600) + 2) * 3600.0
        a = Effective(five_pct=0.0, five_resets=at - 0.19, week_pct=50.0, window="active")
        b = Effective(five_pct=32.0, five_resets=at, week_pct=48.0, window="active")
        items = [(Profile("gmail"), a), (Profile("work"), b)]
        self.assertEqual(self.names(items)[0], ["work", "gmail"], "a tie: lower weekly usage first")
        self.assertEqual(self.names(items, prefer="gmail")[0], ["gmail", "work"], "a tie keeps the current one")
        self.assertEqual(self.names(items, prefer="work")[0], ["work", "gmail"])
        a.five_resets = at - 600  # really sooner: the rule wins over staying
        self.assertEqual(self.names(items, prefer="work")[0], ["gmail", "work"])
        self.assertEqual(picker._hhmm(at - 0.19), picker._hhmm(at), "shown as the same minute")

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
