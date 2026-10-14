import unittest
from datetime import date, datetime, timezone

from build_portal_views import _build_global_portal, _lifecycle_rollup, _market_today


class PortalLifecycleTests(unittest.TestCase):
    def test_lifecycle_rollup_uses_today_fallback_and_future_only_advance(self):
        rollup_dates = {
            "2026-10-12": {
                "current": {
                    "booked_gross": 100,
                    "booked_tickets": 1,
                    "show_count": 2,
                }
            },
            "2026-10-13": {
                "current": {
                    "booked_gross": 0,
                    "booked_tickets": 0,
                    "show_count": 0,
                },
                "advance": {
                    "booked_gross": 400,
                    "booked_tickets": 4,
                    "show_count": 3,
                },
            },
            "2026-10-14": {
                "advance": {
                    "booked_gross": 600,
                    "booked_tickets": 6,
                    "show_count": 5,
                }
            },
            "2026-10-15": {
                "advance": {
                    "booked_gross": 0,
                    "booked_tickets": 0,
                    "show_count": 0,
                }
            },
        }

        result = _lifecycle_rollup(rollup_dates, date(2026, 10, 13))

        self.assertEqual(result["status"], "playing_now")
        self.assertEqual(result["total_gross_so_far"], 500)
        self.assertEqual(result["total_tickets_so_far"], 5)
        self.assertEqual(result["total_shows_so_far"], 5)
        self.assertEqual(result["advance_sales_from_tomorrow"], 600)
        self.assertEqual(result["advance_tickets_from_tomorrow"], 6)
        self.assertEqual(result["valid_dates"], [
            "2026-10-12",
            "2026-10-13",
            "2026-10-14",
        ])

        rollup_dates["2026-10-13"]["current"] = {
            "booked_gross": 300,
            "booked_tickets": 3,
            "show_count": 2,
        }
        preferred_current = _lifecycle_rollup(rollup_dates, date(2026, 10, 13))
        self.assertEqual(preferred_current["total_gross_so_far"], 400)
        self.assertEqual(preferred_current["total_tickets_so_far"], 4)

    def test_market_local_time_and_global_status_precedence(self):
        reference = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(_market_today("india", reference), date(2026, 10, 13))
        self.assertEqual(_market_today("usa", reference), date(2026, 10, 12))

        global_view = _build_global_portal(
            {
                "india": {
                    "currency": "INR",
                    "movie_catalog": [{
                        "global_slug": "shared-title",
                        "title": "Shared Title",
                        "status": "coming_soon",
                        "currency": "INR",
                        "total_gross_so_far": 0,
                        "total_tickets_so_far": 0,
                        "advance_sales_from_tomorrow": 1000,
                        "advance_tickets_from_tomorrow": 10,
                    }],
                },
                "usa": {
                    "currency": "USD",
                    "movie_catalog": [{
                        "global_slug": "shared-title",
                        "title": "Shared Title",
                        "status": "playing_now",
                        "currency": "USD",
                        "total_gross_so_far": 2000,
                        "total_tickets_so_far": 20,
                        "advance_sales_from_tomorrow": 0,
                        "advance_tickets_from_tomorrow": 0,
                    }],
                },
            },
            "2026-10-13T04:00:00Z",
            0.0118,
        )

        title = global_view["global_catalog"][0]
        self.assertEqual(title["status"], "playing_now")
        self.assertEqual(
            title["market_statuses"],
            {"india": "coming_soon", "usa": "playing_now"},
        )
        self.assertEqual(
            global_view["top_spotlight_playing_now"]["global_slug"],
            "shared-title",
        )

    def test_empty_and_stale_dates_do_not_receive_active_status(self):
        today = date(2026, 10, 13)
        self.assertEqual(_lifecycle_rollup({}, today)["status"], "unknown")
        self.assertEqual(
            _lifecycle_rollup(
                {"2026-10-12": {"current": {"booked_gross": 0, "booked_tickets": 0}}},
                today,
            )["status"],
            "unknown",
        )
        self.assertEqual(
            _lifecycle_rollup(
                {"2026-10-12": {"current": {"booked_gross": 1}}},
                today,
            )["status"],
            "archived",
        )


if __name__ == "__main__":
    unittest.main()
