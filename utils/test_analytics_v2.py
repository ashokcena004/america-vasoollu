import unittest

from utils.analytics_v2 import build_analytics_payload


def make_show(time, booked, gross, state="California"):
    return {
        "state": state,
        "city": "Los Angeles",
        "region": "West",
        "t_id": "theater-1",
        "theater": "Example Cinema",
        "format": "IMAX",
        "language": "English",
        "time": time,
        "total": 100,
        "booked": booked,
        "gross": gross,
    }


class AnalyticsV2Tests(unittest.TestCase):
    def build(self, current, previous):
        return build_analytics_payload(
            current,
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-2",
            previous_rows=previous,
        )["current"]

    def test_matches_previous_show_within_one_hour_despite_time_and_state_change(self):
        summary = self.build(
            [make_show("8:45 PM", 20, 200, state="Nevada")],
            [make_show("8:00 PM", 10, 100)],
        )

        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"], {
            "added": 0,
            "removed": 0,
            "tickets_increased": 1,
            "tickets_decreased": 0,
        })
        self.assertEqual(summary["metrics"]["previous_run_delta"]["booked_tickets"], 10)

    def test_does_not_time_match_shows_more_than_one_hour_apart(self):
        summary = self.build(
            [make_show("10:01 PM", 20, 200)],
            [make_show("8:00 PM", 10, 100)],
        )

        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["added"], 1)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["removed"], 1)

    def test_time_match_handles_midnight_boundary(self):
        summary = self.build(
            [make_show("12:20 AM", 20, 200)],
            [make_show("11:50 PM", 10, 100)],
        )

        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["added"], 0)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["removed"], 0)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["tickets_increased"], 1)

    def test_time_match_uses_legacy_premium_dbox_format_normalization(self):
        current = make_show("8:30 PM", 20, 200)
        previous = make_show("8:00 PM", 10, 100)
        current["format"] = "D-Box Premium"
        previous["format"] = "Premium"

        summary = self.build([current], [previous])

        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["added"], 0)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["removed"], 0)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["tickets_increased"], 1)


if __name__ == "__main__":
    unittest.main()
