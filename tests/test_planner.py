import time
import unittest

from cas import planner


class PlannerTest(unittest.TestCase):
    def test_single_account_gets_three_windows(self):
        pl = planner.plan(["a"], planner.parse_hhmm("09:00"), planner.parse_hhmm("19:00"))
        # first window resets at 11:00, then chained every 5h (+2 min so the previous one has reset)
        self.assertEqual([planner.fmt_hhmm(x) for x in pl["a"]], ["06:00", "11:02", "16:04"])

    def test_stagger_offsets_accounts(self):
        pl = planner.plan(["a", "b"], 9 * 60, 19 * 60, "stagger")
        self.assertEqual(pl["a"][0], 6 * 60)
        self.assertEqual(pl["b"][0], 8 * 60 + 30)
        # b's 18:34 window would overlap work hours by 26 min < 60: dropped
        self.assertEqual([planner.fmt_hhmm(x) for x in pl["b"]], ["08:30", "13:32"])

    def test_align_puts_everyone_on_one_chain(self):
        pl = planner.plan(["a", "b", "c"], 9 * 60, 19 * 60, "align")
        self.assertTrue(pl["a"] == pl["b"] == pl["c"])

    def test_early_start_wraps_to_previous_day(self):
        pl = planner.plan(["a"], 2 * 60, 8 * 60, first_reset_after=60)
        self.assertLess(pl["a"][0], 0)  # 22:00 the evening before
        entries = planner.calendar_entries(pl["a"], [1])  # Monday
        self.assertIn({"Weekday": 0, "Hour": 22, "Minute": 0}, entries)  # Sunday 22:00

    def test_parse_days(self):
        self.assertEqual(planner.parse_days("mon-fri"), [1, 2, 3, 4, 5])
        self.assertEqual(planner.parse_days("fri-mon"), [0, 1, 5, 6])
        self.assertEqual(planner.parse_days("daily"), list(range(7)))
        self.assertEqual(planner.parse_days("mon,wed"), [1, 3])

    def test_worth_pinging(self):
        anchors = [6 * 60, 11 * 60 + 2]
        days = [1, 2, 3, 4, 5]
        monday_0700 = time.struct_time((2026, 11, 23, 7, 0, 0, 0, 327, 0))
        monday_2000 = time.struct_time((2026, 11, 23, 20, 0, 0, 0, 327, 0))
        sunday_0700 = time.struct_time((2026, 11, 22, 7, 0, 0, 6, 326, 0))
        self.assertTrue(planner.worth_pinging(monday_0700, days, anchors, 540, 1140))
        self.assertFalse(planner.worth_pinging(monday_2000, days, anchors, 540, 1140))
        self.assertFalse(planner.worth_pinging(sunday_0700, days, anchors, 540, 1140))

    def test_render_has_a_row_per_profile(self):
        pl = planner.plan(["a", "b"], 540, 1140)
        out = planner.render(pl, 540, 1140)
        self.assertEqual(len(out.splitlines()), 4)
        self.assertIn("pings 06:00", out)


if __name__ == "__main__":
    unittest.main()
