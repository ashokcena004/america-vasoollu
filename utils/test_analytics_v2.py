import unittest

from utils.analytics_v2 import build_analytics_payload, publish_analytics_payload


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


class FakeReference:
    def __init__(self, database, path):
        self.database = database
        self.path = path

    def get(self):
        return self.database.values.get(self.path)

    def set(self, value):
        self.database.values[self.path] = value
        self.database.writes.append(("set", self.path))

    def update(self, value):
        self.database.values.setdefault(self.path, {}).update(value)
        self.database.writes.append(("update", self.path))

    def delete(self):
        self.database.values.pop(self.path, None)
        self.database.writes.append(("delete", self.path))


class FakeDatabase:
    def __init__(self):
        self.values = {}
        self.writes = []

    def reference(self, path):
        return FakeReference(self, path)


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
        self.assertEqual(
            summary["growth"]["previous_run"]["metrics"]["booked_tickets"],
            10,
        )

    def test_does_not_time_match_shows_more_than_one_hour_apart(self):
        summary = self.build(
            [make_show("10:01 PM", 20, 200)],
            [make_show("8:00 PM", 10, 100)],
        )

        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["added"], 1)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["removed"], 1)

    def test_seat_map_identity_preserves_same_time_shows_and_round_trips(self):
        current = [
            dict(make_show("7:30 PM", 25, 250), seat_map_urls="https://example.invalid/map/a"),
            dict(make_show("7:30 PM", 35, 350), seat_map_urls="https://example.invalid/map/b"),
        ]
        previous = [
            dict(make_show("7:30 PM", 20, 200), seat_map_urls="https://example.invalid/map/a"),
            dict(make_show("7:30 PM", 30, 300), seat_map_urls="https://example.invalid/map/b"),
        ]

        first_payload = build_analytics_payload(
            current,
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=previous,
        )
        summary = first_payload["current"]
        persisted_baseline = [
            {**row, "_analytics_show_id": show_id}
            for show_id, row in first_payload["comparison_baseline"]["shows"].items()
        ]
        second_payload = build_analytics_payload(
            current,
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-next",
            previous_rows=persisted_baseline,
        )

        self.assertEqual(len(first_payload["details"]), 2)
        self.assertEqual(len(first_payload["comparison_baseline"]["shows"]), 2)
        self.assertEqual(summary["metrics"]["show_count"], 2)
        self.assertEqual(summary["growth"]["previous_run"]["metrics"]["booked_gross"], 100)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["added"], 0)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["removed"], 0)
        self.assertEqual(
            second_payload["current"]["comparison"]["previous_run"]["change_counts"]["added"],
            0,
        )
        self.assertEqual(
            second_payload["current"]["comparison"]["previous_run"]["change_counts"]["removed"],
            0,
        )

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

    def test_summary_has_complete_tables_with_both_growth_modes_and_compact_history(self):
        current = []
        previous = []
        daily = []
        for index in range(25):
            current_show = make_show("8:00 PM", index + 20, (index + 20) * 10)
            current_show.update({
                "t_id": f"theater-{index}",
                "theater": f"Theater {index}",
                "state": f"State {index}",
            })
            previous_show = dict(current_show, booked=index + 10, gross=(index + 10) * 10)
            daily_show = dict(current_show, booked=index + 5, gross=(index + 5) * 10)
            current.append(current_show)
            previous.append(previous_show)
            daily.append(daily_show)

        payload = build_analytics_payload(
            current,
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=previous,
            daily_baseline_rows=daily,
            previous_run_id="run-previous",
            daily_snapshot_id="run-snapshot",
        )
        summary = payload["current"]
        theater_rows = summary["tables"]["theaters"]
        first_theater = next(row for row in theater_rows if row["id"] == "theater-0")

        self.assertEqual(len(theater_rows), 25)
        self.assertEqual(first_theater["gross_delta_previous_run"], 100)
        self.assertEqual(first_theater["gross_delta_daily_snapshot"], 150)
        self.assertEqual(
            summary["growth"]["previous_run"]["base_run_id"],
            "run-previous",
        )
        self.assertEqual(
            summary["growth"]["daily_snapshot"]["base_snapshot_id"],
            "run-snapshot",
        )
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["added"], 0)
        self.assertEqual(summary["comparison"]["daily_snapshot"]["change_counts"]["added"], 0)
        self.assertNotIn("dimensions", summary)
        self.assertNotIn("dimension_pages", payload)
        self.assertNotIn("daily_dimension_pages", payload)
        self.assertEqual(
            set(payload["history_entry"]),
            {"timestamp", "shows", "booked_tickets", "total_gross", "occupancy"},
        )

    def test_show_details_keep_dashboard_fields_and_both_comparison_deltas(self):
        current = make_show("8:00 PM", 20, 200)
        current.update({
            "status": "Available",
            "price_str": "$12.00",
            "seat_map_urls": ["https://example.invalid/seat-map"],
        })
        previous = make_show("8:00 PM", 15, 150)
        daily = make_show("8:00 PM", 10, 100)

        payload = build_analytics_payload(
            [current],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=[previous],
            daily_baseline_rows=[daily],
        )
        detail = next(iter(payload["details"].values()))

        self.assertEqual(detail["state"], "California")
        self.assertEqual(detail["theater"], "Example Cinema")
        self.assertEqual(detail["status"], "Available")
        self.assertEqual(detail["price_str"], "$12.00")
        self.assertEqual(detail["gross_delta_previous_run"], 50)
        self.assertEqual(detail["gross_delta_daily_snapshot"], 100)
        self.assertEqual(detail["tickets_delta_previous_run"], 5)
        self.assertEqual(detail["tickets_delta_daily_snapshot"], 10)
        self.assertNotIn("theater_name", detail)
        self.assertNotIn("region_id", detail)
        self.assertNotIn("seat_map_urls", detail)

    def test_manual_extra_gross_is_included_in_previous_run_baseline(self):
        current = make_show("8:00 PM", 20, 200)
        previous = make_show("8:00 PM", 10, 100)
        current_extra = {
            "state": "EXTRA",
            "t_id": "EXTRA",
            "theater": "EXTRA",
            "format": "EXTRA",
            "language": "EXTRA",
            "time": "",
            "total": 100,
            "booked": 100,
            "gross": 19000,
            "is_extra": True,
        }
        previous_extra = dict(current_extra)

        payload = build_analytics_payload(
            [current, current_extra],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=[previous, previous_extra],
            daily_baseline_rows=[previous, previous_extra],
        )

        self.assertEqual(
            payload["current"]["growth"]["previous_run"]["metrics"]["booked_gross"],
            100,
        )
        self.assertEqual(
            payload["current"]["growth"]["daily_snapshot"]["metrics"]["booked_gross"],
            100,
        )
        self.assertEqual(payload["comparison_baseline"]["extras"], [current_extra])
        self.assertEqual(
            sum(row["gross_delta"] for row in payload["change_pages"]["previous_run"]["tickets_increased"]),
            100,
        )
        for dimension_rows in payload["current"]["tables"].values():
            self.assertEqual(
                sum(row["booked_gross"] for row in dimension_rows),
                payload["current"]["metrics"]["booked_gross"],
            )
            self.assertEqual(
                sum(row["gross_delta_previous_run"] for row in dimension_rows),
                payload["current"]["growth"]["previous_run"]["metrics"]["booked_gross"],
            )
        database = FakeDatabase()
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            payload,
            update_daily_baseline=True,
        )
        self.assertEqual(
            database.values[
                "markets/usa/movies/example-film/dates/2026-10-10/comparison_baselines/daily"
            ]["extras"],
            [current_extra],
        )

    def test_removed_extra_adjustment_reconciles_all_dimension_deltas(self):
        current = make_show("8:00 PM", 20, 200)
        previous = make_show("8:00 PM", 15, 150)
        previous_extra = {
            "state": "EXTRA",
            "t_id": "EXTRA",
            "theater": "EXTRA",
            "format": "EXTRA",
            "language": "EXTRA",
            "time": "",
            "total": 1072,
            "booked": 726,
            "gross": 21509.54,
            "is_extra": True,
        }

        payload = build_analytics_payload(
            [current],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=[previous, previous_extra],
            daily_baseline_rows=[previous, previous_extra],
        )
        summary = payload["current"]
        expected_gross_delta = summary["growth"]["previous_run"]["metrics"]["booked_gross"]
        adjustment_id = "unattributed-adjustments"

        self.assertAlmostEqual(expected_gross_delta, -21459.54)
        for dimension_rows in summary["tables"].values():
            self.assertAlmostEqual(
                sum(row["gross_delta_previous_run"] for row in dimension_rows),
                expected_gross_delta,
            )
            self.assertAlmostEqual(
                sum(row["gross_delta_daily_snapshot"] for row in dimension_rows),
                summary["growth"]["daily_snapshot"]["metrics"]["booked_gross"],
            )
            self.assertAlmostEqual(
                sum(row["booked_gross"] for row in dimension_rows),
                summary["metrics"]["booked_gross"],
            )
            adjustment = next(row for row in dimension_rows if row["id"] == adjustment_id)
            self.assertEqual(adjustment["name"], "Unattributed Adjustments")
            self.assertEqual(adjustment["booked_gross"], 0)
            self.assertEqual(adjustment["gross_delta_previous_run"], -21509.54)
            self.assertEqual(adjustment["tickets_delta_previous_run"], -726)

    def test_dimension_growth_includes_entities_removed_since_previous_run(self):
        current_show = make_show("8:00 PM", 20, 200)
        previous_matching_show = make_show("8:00 PM", 15, 150)
        previous_removed_show = make_show("9:00 PM", 10, 100)
        current_show["format"] = "Standard"
        previous_matching_show["format"] = "Standard"
        previous_removed_show["format"] = "D-Box"
        current_show["language"] = "Tamil"
        previous_matching_show["language"] = "Tamil"
        previous_removed_show["language"] = "Telugu"
        daily_matching_show = make_show("8:00 PM", 12, 120)
        daily_removed_show = make_show("9:30 PM", 8, 80)
        daily_matching_show["format"] = "Standard"
        daily_removed_show["format"] = "Premium"
        daily_matching_show["language"] = "Tamil"
        daily_removed_show["language"] = "Hindi"

        payload = build_analytics_payload(
            [current_show],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=[previous_matching_show, previous_removed_show],
            daily_baseline_rows=[daily_matching_show, daily_removed_show],
        )
        summary = payload["current"]
        expected_gross_delta = summary["growth"]["previous_run"]["metrics"]["booked_gross"]
        expected_daily_gross_delta = summary["growth"]["daily_snapshot"]["metrics"]["booked_gross"]

        for dimension in ("formats", "languages"):
            rows = summary["tables"][dimension]
            self.assertEqual(sum(row["gross_delta_previous_run"] for row in rows), expected_gross_delta)
            self.assertEqual(sum(row["gross_delta_daily_snapshot"] for row in rows), expected_daily_gross_delta)
            self.assertEqual(sum(row["booked_gross"] for row in rows), summary["metrics"]["booked_gross"])

        removed_format = next(
            row for row in summary["tables"]["formats"] if row["name"] == "D-Box"
        )
        self.assertEqual(removed_format["booked_gross"], 0)
        self.assertEqual(removed_format["gross_delta_previous_run"], -100)
        daily_only_format = next(
            row for row in summary["tables"]["formats"] if row["name"] == "Premium"
        )
        self.assertEqual(daily_only_format["booked_gross"], 0)
        self.assertEqual(daily_only_format["gross_delta_daily_snapshot"], -80)

    def test_publisher_writes_only_daily_snapshot_and_preserves_distinct_advance_run(self):
        database = FakeDatabase()
        root = "markets/usa/movies/example-film/dates/2026-10-10"
        first_payload = build_analytics_payload(
            [make_show("8:00 PM", 10, 100)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="advance-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            first_payload,
            update_daily_baseline=True,
            update_advance_snapshot=True,
        )
        second_payload = build_analytics_payload(
            [make_show("8:00 PM", 20, 200)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="active-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            second_payload,
            update_daily_baseline=False,
            update_advance_snapshot=False,
        )

        pointer = database.values[f"{root}/published"]
        self.assertEqual(pointer["active_run_id"], "active-run")
        self.assertEqual(pointer["advance_run_id"], "advance-run")
        self.assertEqual(pointer["active_detail_count"], 1)
        self.assertEqual(pointer["advance_detail_count"], 1)
        self.assertTrue(pointer["advance_details_available"])
        self.assertIn(f"{root}/movie_details/runs/advance-run/pages/0001", database.values)
        self.assertIn(f"{root}/analytics_v2/runs/advance-run/summary", database.values)
        self.assertIn(f"{root}/comparison_baselines/daily", database.values)
        self.assertNotIn(f"{root}/comparison_baselines/previous_run", database.values)
        self.assertNotIn(f"{root}/analytics_v2/runs/active-run/pages/dimensions", database.values)
        self.assertEqual(
            set(database.values[f"{root}/history/active-run"]),
            {"timestamp", "shows", "booked_tickets", "total_gross", "occupancy"},
        )

    def test_publisher_does_not_use_active_counts_for_legacy_distinct_advance_run(self):
        database = FakeDatabase()
        root = "markets/usa/movies/example-film/dates/2026-10-10"
        database.reference(f"{root}/published").set({
            "active_run_id": "active-old",
            "advance_run_id": "advance-old",
            "detail_count": 25,
            "detail_page_count": 2,
        })
        database.reference(
            f"{root}/analytics_v2/runs/advance-old/summary"
        ).set({"metrics": {"show_count": 1}})
        payload = build_analytics_payload(
            [make_show("8:00 PM", 10, 100)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="active-new",
        )

        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            payload,
            update_advance_snapshot=False,
        )

        pointer = database.values[f"{root}/published"]
        self.assertEqual(pointer["advance_detail_count"], 1)
        self.assertEqual(pointer["advance_detail_page_count"], 1)


if __name__ == "__main__":
    unittest.main()
