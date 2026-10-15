import unittest
from copy import deepcopy
from datetime import date, datetime, timezone

from build_portal_views import (
    _build_global_portal,
    _lifecycle_rollup,
    _market_today,
    build_portal_views,
)


class FakeReference:
    def __init__(self, database, path):
        self.database = database
        self.path = path

    def get(self):
        return deepcopy(self.database.values.get(self.path))

    def set(self, value):
        self.database.values[self.path] = deepcopy(value)


class FakeDatabase:
    def __init__(self):
        self.values = {}

    def reference(self, path):
        return FakeReference(self, path)


class PortalLifecycleTests(unittest.TestCase):
    def test_today_advance_is_provisional_current_data_until_scrape_publishes(self):
        database = FakeDatabase()
        movie_id = "today-presales"
        movie_root = f"markets/usa/movies/{movie_id}"
        date_key = "2026-10-13"
        run_id = f"{movie_id}-{date_key}-advance"
        database.values["markets/usa/movie_index"] = {
            movie_id: {"movie_id": movie_id, "name": "Today Presales"}
        }
        database.values[f"{movie_root}/meta"] = {
            "title": "Today Presales",
            "global_slug": movie_id,
            "currency": "USD",
        }
        database.values[f"{movie_root}/date_index"] = {
            date_key: {"has_advance": True}
        }
        database.values[f"{movie_root}/cumulative/v1/bases"] = {
            "fandango_reported": {
                "currency": "USD",
                "months": {
                    "2026-10": {
                        "dates": {
                            date_key: {
                                "advance": {
                                    "run_id": run_id,
                                    "booked_gross": 1000,
                                    "booked_tickets": 10,
                                    "show_count": 2,
                                }
                            }
                        }
                    }
                },
            }
        }
        database.values[f"{movie_root}/dates/{date_key}/published"] = {
            "advance_run_id": run_id
        }
        database.values[
            f"{movie_root}/dates/{date_key}/analytics_v2/runs/{run_id}/summary"
        ] = {
            "metrics": {
                "booked_gross": 1000,
                "booked_tickets": 10,
                "capacity_seats": 25,
                "occupancy_rate": 40,
            }
        }

        result = build_portal_views(
            database,
            market_ids=("usa",),
            as_of_date=date_key,
            updated_at="2026-10-13T00:01:00Z",
        )
        movie = result["markets"]["usa"]["movie_catalog"][0]

        self.assertEqual(movie["status"], "playing_now")
        self.assertEqual(movie["total_gross_so_far"], 1000)
        self.assertEqual(movie["current"]["total_gross"], 1000)
        self.assertEqual(movie["current"]["total_tickets"], 10)
        self.assertEqual(movie["current"]["total_shows"], 2)
        self.assertAlmostEqual(movie["current"]["avg_occupancy"], 0.4)
        self.assertTrue(movie["current"]["avg_occupancy_is_estimate"])
        summary = database.values[f"{movie_root}/summary_view"]
        self.assertEqual(
            [point["date"] for point in summary["daily_trajectory_current"]],
            [date_key],
        )
        self.assertEqual(summary["daily_trajectory_current"][0]["daily_gross"], 1000)
        self.assertTrue(summary["date_index"][date_key]["has_current"])
        self.assertTrue(summary["date_index"][date_key]["has_advance"])

        database.values[
            f"{movie_root}/cumulative/v1/bases"
        ]["fandango_reported"]["months"]["2026-10"]["dates"][date_key]["current"] = {
            "booked_gross": 0,
            "booked_tickets": 0,
            "show_count": 0,
        }
        database.values[f"{movie_root}/date_index"][date_key]["has_current"] = True
        zero_run_id = f"{movie_id}-{date_key}-empty-current"
        database.values[f"{movie_root}/dates/{date_key}/published"][
            "active_run_id"
        ] = zero_run_id
        database.values[
            f"{movie_root}/dates/{date_key}/analytics_v2/runs/{zero_run_id}/summary"
        ] = {"metrics": {"booked_gross": 0, "booked_tickets": 0}}

        zero_scrape_views = build_portal_views(
            database,
            market_ids=("usa",),
            as_of_date=date_key,
            updated_at="2026-10-13T12:00:00Z",
        )
        zero_scrape_movie = zero_scrape_views["markets"]["usa"]["movie_catalog"][0]
        zero_scrape_summary = database.values[f"{movie_root}/summary_view"]
        self.assertEqual(zero_scrape_movie["current"]["total_gross"], 0)
        self.assertEqual(
            zero_scrape_summary["daily_trajectory_current"][0]["daily_gross"],
            0,
        )

        database.values[
            f"{movie_root}/cumulative/v1/bases"
        ]["fandango_reported"]["months"]["2026-10"]["dates"][date_key]["current"] = {
            "booked_gross": 1500,
            "booked_tickets": 15,
            "show_count": 3,
        }
        database.values[f"{movie_root}/date_index"][date_key]["has_current"] = True
        current_run_id = f"{movie_id}-{date_key}-current"
        database.values[f"{movie_root}/dates/{date_key}/published"][
            "active_run_id"
        ] = current_run_id
        database.values[
            f"{movie_root}/dates/{date_key}/analytics_v2/runs/{current_run_id}/summary"
        ] = {
            "metrics": {
                "booked_gross": 1500,
                "booked_tickets": 15,
                "capacity_seats": 50,
                "occupancy_rate": 30,
            }
        }

        refreshed = build_portal_views(
            database,
            market_ids=("usa",),
            as_of_date=date_key,
            updated_at="2026-10-13T18:00:00Z",
        )
        movie_after_scrape = refreshed["markets"]["usa"]["movie_catalog"][0]
        self.assertEqual(movie_after_scrape["current"]["total_gross"], 1500)
        self.assertAlmostEqual(movie_after_scrape["current"]["avg_occupancy"], 0.3)
        self.assertFalse(
            movie_after_scrape["current"]["avg_occupancy_is_estimate"]
        )
        refreshed_summary = database.values[f"{movie_root}/summary_view"]
        self.assertEqual(
            refreshed_summary["daily_trajectory_current"][0]["daily_gross"],
            1500,
        )
        self.assertEqual(refreshed_summary["lifetime_kpis"]["advance_gross"], 0)
        self.assertEqual(
            refreshed_summary["daily_trajectory_advance"][0]["advance_gross"],
            1000,
        )

    def test_past_advance_only_date_moves_into_current_totals_and_trajectory(self):
        database = FakeDatabase()
        movie_id = "missed-scrape"
        movie_root = f"markets/usa/movies/{movie_id}"
        date_key = "2026-10-12"
        run_id = f"{movie_id}-{date_key}-advance"
        database.values["markets/usa/movie_index"] = {
            movie_id: {"movie_id": movie_id, "name": "Missed Scrape"}
        }
        database.values[f"{movie_root}/meta"] = {
            "title": "Missed Scrape",
            "global_slug": movie_id,
            "currency": "USD",
        }
        database.values[f"{movie_root}/date_index"] = {
            date_key: {"has_advance": True}
        }
        database.values[f"{movie_root}/cumulative/v1/bases"] = {
            "fandango_reported": {
                "currency": "USD",
                "months": {
                    "2026-10": {
                        "dates": {
                            date_key: {
                                "advance": {
                                    "run_id": run_id,
                                    "booked_gross": 750,
                                    "booked_tickets": 8,
                                    "show_count": 2,
                                }
                            }
                        }
                    }
                },
            }
        }
        database.values[f"{movie_root}/dates/{date_key}/published"] = {
            "advance_run_id": run_id
        }
        database.values[
            f"{movie_root}/dates/{date_key}/analytics_v2/runs/{run_id}/summary"
        ] = {
            "metrics": {
                "booked_gross": 750,
                "booked_tickets": 8,
                "capacity_seats": 20,
                "occupancy_rate": 40,
                "show_count": 2,
            }
        }

        result = build_portal_views(
            database,
            market_ids=("usa",),
            as_of_date="2026-10-13",
            updated_at="2026-10-13T18:00:00Z",
        )
        movie = result["markets"]["usa"]["movie_catalog"][0]
        summary = database.values[f"{movie_root}/summary_view"]

        self.assertEqual(movie["current"]["total_gross"], 750)
        self.assertEqual(movie["current"]["total_tickets"], 8)
        self.assertEqual(movie["current"]["total_shows"], 2)
        self.assertEqual(movie["advance"]["advance_gross"], 0)
        self.assertEqual(
            [point["date"] for point in summary["daily_trajectory_current"]],
            [date_key],
        )
        self.assertEqual(summary["daily_trajectory_current"][0]["shows"], 2)
        self.assertEqual(
            summary["daily_trajectory_current"][0]["capacity_seats"],
            20,
        )
        self.assertEqual(
            [point["date"] for point in summary["daily_trajectory_advance"]],
            [date_key],
        )
        self.assertTrue(summary["date_index"][date_key]["has_current"])
        self.assertTrue(summary["date_index"][date_key]["has_advance"])

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
