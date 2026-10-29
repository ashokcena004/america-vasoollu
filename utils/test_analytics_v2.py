import unittest
from copy import deepcopy

from utils.analytics_v2 import (
    build_analytics_payload,
    build_combined_format_summary,
    build_format_summary,
    classify_report_formats,
    combine_report_format_categories,
    publish_analytics_payload,
)


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
        self.database.updates.append((self.path, dict(value)))
        if any("/" in key for key in value):
            for key, child in value.items():
                child_path = f"{self.path}/{key}".strip("/")
                if child is None:
                    self.database.values.pop(child_path, None)
                else:
                    self.database.values[child_path] = child
        else:
            self.database.values.setdefault(self.path, {}).update(value)
        self.database.writes.append(("update", self.path))

    def delete(self):
        self.database.values.pop(self.path, None)
        self.database.writes.append(("delete", self.path))

    def transaction(self, update_function):
        current = deepcopy(self.database.values.get(self.path))
        updated = update_function(current)
        self.database.values[self.path] = deepcopy(updated)
        self.database.writes.append(("transaction", self.path))
        return deepcopy(updated)


class FakeDatabase:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.updates = []

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

    def test_report_format_classifier_uses_overlapping_screen_facets_and_ignores_dbox(self):
        self.assertEqual(classify_report_formats("IMAX 3D, D-BOX"), ["IMAX", "3D"])
        self.assertEqual(classify_report_formats("Cinemark XD / D-BOX"), ["Cinemark XD"])
        self.assertEqual(classify_report_formats("RPX 3D"), ["3D", "RPX"])
        self.assertEqual(classify_report_formats("Laser at AMC"), ["Laser at AMC"])
        self.assertEqual(classify_report_formats("4DX"), ["Other Premium"])
        self.assertEqual(classify_report_formats("D-BOX"), ["Standard"])
        self.assertEqual(
            classify_report_formats("4DX amenity", base_format="Standard"),
            ["Standard"],
        )
        self.assertEqual(
            classify_report_formats("unlisted format", base_format="Premium"),
            ["Other Premium"],
        )
        self.assertEqual(
            classify_report_formats("unknown amenity", base_format="Laser"),
            ["Other Premium"],
        )
        self.assertEqual(
            combine_report_format_categories([
                {"format": "Cinemark XD", "format_categories": ["Cinemark XD"]},
                {"format": "D-BOX", "format_categories": []},
            ]),
            ["Cinemark XD"],
        )

    def test_overlapping_format_totals_keep_show_matching_and_category_growth_separate(self):
        current = make_show("8:00 PM", 10, 120)
        previous = make_show("8:00 PM", 8, 100)
        current["format"] = previous["format"] = "IMAX 3D"
        current["format_categories"] = ["3D"]
        previous["format_categories"] = ["IMAX"]

        payload = build_analytics_payload(
            [current],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=[previous],
        )
        summary = payload["current"]
        format_rows = {row["name"]: row for row in summary["tables"]["formats"]}
        detail = next(iter(payload["details"].values()))

        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["added"], 0)
        self.assertEqual(summary["comparison"]["previous_run"]["change_counts"]["removed"], 0)
        self.assertEqual(detail["format"], "IMAX 3D")
        self.assertEqual(detail["format_name"], "3D")
        self.assertEqual(detail["format_categories_previous_run"], ["IMAX"])
        self.assertEqual(format_rows["IMAX"]["booked_gross"], 0)
        self.assertEqual(format_rows["IMAX"]["gross_delta_previous_run"], -100)
        self.assertEqual(format_rows["3D"]["booked_gross"], 120)
        self.assertEqual(format_rows["3D"]["gross_delta_previous_run"], 120)
        self.assertEqual(summary["metrics"]["booked_gross"], 120)

    def test_report_format_summary_duplicates_full_metrics_for_each_facet(self):
        current = make_show("8:00 PM", 10, 120)
        previous = make_show("8:00 PM", 8, 100)
        current["format"] = previous["format"] = "IMAX 3D"

        formats = build_format_summary([current], [previous])
        self.assertEqual(formats["IMAX"]["booked_gross"], 120)
        self.assertEqual(formats["3D"]["booked_gross"], 120)
        self.assertEqual(formats["IMAX"]["gross_delta"], 20)
        self.assertEqual(formats["3D"]["gross_delta"], 20)

        payload = build_analytics_payload(
            [current],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-formats",
            previous_rows=[previous],
        )
        database = FakeDatabase()
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            payload,
        )

        root = "markets/usa/movies/example-film/dates/2026-10-10"
        persisted_summary = database.values[f"{root}/analytics_v2/runs/run-formats/summary"]
        persisted_detail = database.values[
            f"{root}/movie_details/runs/run-formats/pages/0001"
        ]
        detail = next(iter(persisted_detail.values()))
        persisted_formats = {row["name"]: row for row in persisted_summary["tables"]["formats"]}
        self.assertEqual(detail["format"], "IMAX 3D")
        self.assertEqual(detail["format_name"], "IMAX / 3D")
        self.assertEqual(detail["format_categories"], ["IMAX", "3D"])
        self.assertEqual(persisted_formats["IMAX"]["booked_gross"], 120)
        self.assertEqual(persisted_formats["3D"]["booked_gross"], 120)

    def test_combined_format_summary_groups_exact_combinations_once(self):
        current_imax_3d = make_show("8:00 PM", 10, 120)
        current_imax = make_show("9:00 PM", 5, 60)
        previous_imax_3d = make_show("8:00 PM", 8, 100)
        for show in (current_imax_3d, previous_imax_3d):
            show["format"] = "IMAX 3D"
        current_imax["format"] = "IMAX"

        combinations = {
            row["name"]: row
            for row in build_combined_format_summary(
                [current_imax_3d, current_imax],
                [previous_imax_3d],
            )
        }
        self.assertEqual(combinations["IMAX / 3D"]["show_count"], 1)
        self.assertEqual(combinations["IMAX / 3D"]["booked_gross"], 120)
        self.assertEqual(combinations["IMAX / 3D"]["gross_delta"], 20)
        self.assertEqual(combinations["IMAX"]["show_count"], 1)
        self.assertEqual(combinations["IMAX"]["booked_gross"], 60)

        payload = build_analytics_payload(
            [current_imax_3d, current_imax],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-combinations",
            previous_rows=[previous_imax_3d],
        )
        combined_rows = {
            row["name"]: row
            for row in payload["current"]["tables"]["format_combinations"]
        }
        self.assertEqual(combined_rows["IMAX / 3D"]["gross_delta_previous_run"], 20)
        self.assertEqual(combined_rows["IMAX"]["gross_delta_previous_run"], 60)

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

    def test_breakdown_growth_matches_duplicate_show_keys_one_to_one(self):
        current = [
            dict(make_show("7:30 PM", 25, 250), seat_map_urls="https://example.invalid/map/a"),
            dict(make_show("7:30 PM", 35, 350), seat_map_urls="https://example.invalid/map/b"),
        ]
        previous = [
            dict(make_show("7:30 PM", 20, 200), seat_map_urls="https://example.invalid/map/a"),
            dict(make_show("7:30 PM", 30, 300), seat_map_urls="https://example.invalid/map/b"),
        ]

        payload = build_analytics_payload(
            current,
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="run-current",
            previous_rows=previous,
        )
        summary = payload["current"]
        for dimension_rows in summary["tables"].values():
            self.assertEqual(
                sum(row["gross_delta_previous_run"] for row in dimension_rows),
                100,
            )
            self.assertEqual(
                sum(row["tickets_delta_previous_run"] for row in dimension_rows),
                10,
            )
        self.assertEqual(
            summary["growth"]["previous_run"]["metrics"]["booked_gross"],
            100,
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
            self.assertEqual(len(dimension_rows), 1)
            self.assertEqual(
                sum(row["gross_delta_previous_run"] for row in dimension_rows),
                100,
            )
            self.assertNotIn(
                "Unattributed Adjustments",
                [row["name"] for row in dimension_rows],
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

    def test_removed_extra_is_excluded_from_dimension_growth(self):
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
        self.assertAlmostEqual(expected_gross_delta, -21459.54)
        for dimension_rows in summary["tables"].values():
            self.assertEqual(len(dimension_rows), 1)
            self.assertAlmostEqual(sum(
                row["gross_delta_previous_run"] for row in dimension_rows
            ), 50)
            self.assertAlmostEqual(
                sum(row["gross_delta_daily_snapshot"] for row in dimension_rows),
                50,
            )
            self.assertNotIn(
                "Unattributed Adjustments",
                [row["name"] for row in dimension_rows],
            )

    def test_format_growth_compares_category_membership_without_changing_other_dimensions(self):
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
        format_rows = summary["tables"]["formats"]
        self.assertEqual(len(format_rows), 1)
        self.assertEqual(format_rows[0]["name"], "Standard")
        self.assertEqual(format_rows[0]["gross_delta_previous_run"], -50)
        self.assertEqual(format_rows[0]["gross_delta_daily_snapshot"], 80)

        language_rows = summary["tables"]["languages"]
        self.assertEqual(len(language_rows), 1)
        self.assertEqual(language_rows[0]["name"], "Tamil")
        self.assertEqual(language_rows[0]["gross_delta_previous_run"], 50)
        self.assertEqual(language_rows[0]["gross_delta_daily_snapshot"], 80)

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
            as_of_date="2026-10-09",
        )
        self.assertEqual(
            database.values[
                "markets/usa/movies/example-film/date_index/2026-10-10"
            ],
            {"has_current": False, "has_advance": True},
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
            as_of_date="2026-10-10",
        )

        pointer = database.values[f"{root}/published"]
        self.assertEqual(pointer["active_run_id"], "active-run")
        self.assertEqual(pointer["advance_run_id"], "advance-run")
        self.assertEqual(
            database.values[
                "markets/usa/movies/example-film/date_index/2026-10-10"
            ],
            {"has_current": True, "has_advance": True},
        )
        same_day_rollup = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/fandango_reported/months/2026-10"
        ]["dates"]["2026-10-10"]
        self.assertEqual(same_day_rollup["current"]["booked_gross"], 200)
        self.assertEqual(same_day_rollup["advance"]["booked_gross"], 100)
        active_pulse = database.values[
            "markets/usa/pulse/v1/dates/2026-10-10/active/example-film"
        ]
        advance_pulse = database.values[
            "markets/usa/pulse/v1/dates/2026-10-10/advance/example-film"
        ]
        self.assertEqual(active_pulse["run_id"], "active-run")
        self.assertEqual(active_pulse["booked_gross"], 200)
        self.assertEqual(active_pulse["gross_basis"], "fandango_reported")
        self.assertEqual(advance_pulse["run_id"], "advance-run")
        self.assertEqual(advance_pulse["booked_gross"], 100)
        market_updates = [
            updates for path, updates in database.updates if path == "markets/usa"
        ]
        self.assertEqual(len(market_updates), 2)
        self.assertIn(
            "movies/example-film/dates/2026-10-10/published",
            market_updates[-1],
        )
        self.assertIn(
            "movies/example-film/date_index/2026-10-10",
            market_updates[-1],
        )
        self.assertIn(
            "pulse/v1/dates/2026-10-10/active/example-film",
            market_updates[-1],
        )
        self.assertNotIn(
            "pulse/v1/dates/2026-10-10/advance/example-film",
            market_updates[-1],
        )
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

    def test_first_same_day_run_is_also_published_as_advance(self):
        database = FakeDatabase()
        root = "markets/usa/movies/example-film/dates/2026-10-10"
        first_payload = build_analytics_payload(
            [make_show("8:00 PM", 10, 100)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="first-run",
        )

        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            first_payload,
            update_advance_snapshot=False,
            as_of_date="2026-10-10",
        )

        pointer = database.values[f"{root}/published"]
        self.assertEqual(pointer["active_run_id"], "first-run")
        self.assertEqual(pointer["advance_run_id"], "first-run")
        self.assertTrue(pointer["advance_details_available"])
        self.assertEqual(
            database.values[
                "markets/usa/movies/example-film/date_index/2026-10-10"
            ],
            {"has_current": True, "has_advance": True},
        )
        self.assertEqual(
            database.values[
                "markets/usa/pulse/v1/dates/2026-10-10/advance/example-film"
            ]["run_id"],
            "first-run",
        )
        snapshot = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/"
            "fandango_reported/months/2026-10"
        ]["dates"]["2026-10-10"]
        self.assertEqual(snapshot["current"]["booked_gross"], 100)
        self.assertEqual(snapshot["advance"]["booked_gross"], 100)

        second_payload = build_analytics_payload(
            [make_show("8:00 PM", 20, 200)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="second-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            second_payload,
            update_advance_snapshot=False,
            as_of_date="2026-10-10",
        )
        pointer = database.values[f"{root}/published"]
        self.assertEqual(pointer["active_run_id"], "second-run")
        self.assertEqual(pointer["advance_run_id"], "first-run")

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
            f"{root}/analytics_v2/runs/advance-old/summary/metrics"
        ).set({"show_count": 1})
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

    def test_cumulative_rollup_replaces_corrected_daily_snapshot_idempotently(self):
        database = FakeDatabase()
        first_payload = build_analytics_payload(
            [make_show("8:00 PM", 10, 100)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="first-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            first_payload,
            as_of_date="2026-10-12",
        )

        corrected_payload = build_analytics_payload(
            [make_show("8:00 PM", 20, 200)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="corrected-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            corrected_payload,
            as_of_date="2026-10-12",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            corrected_payload,
            as_of_date="2026-10-12",
        )

        summary = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/fandango_reported/summary"
        ]
        month = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/fandango_reported/months/2026-10"
        ]
        self.assertEqual(summary["aggregation"], "latest_observed_daily_snapshots")
        self.assertEqual(summary["metrics"]["booked_gross"], 200)
        self.assertEqual(summary["metrics"]["booked_tickets"], 20)
        self.assertEqual(summary["metrics"]["observed_show_dates"], 1)
        self.assertEqual(month["metrics"]["booked_gross"], 200)
        self.assertEqual(month["revision"], 2)

    def test_cumulative_rollup_excludes_future_show_dates(self):
        database = FakeDatabase()
        payload = build_analytics_payload(
            [make_show("8:00 PM", 10, 100)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-15",
            run_id="advance-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-15",
            payload,
            update_advance_snapshot=True,
            as_of_date="2026-10-12",
        )

        summary = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/fandango_reported/summary"
        ]
        month = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/fandango_reported/months/2026-10"
        ]
        self.assertEqual(summary["metrics"]["booked_gross"], 0)
        self.assertEqual(summary["metrics"]["observed_show_dates"], 0)
        self.assertEqual(summary["advance_metrics"]["booked_gross"], 100)
        self.assertEqual(summary["advance_metrics"]["observed_show_dates"], 1)
        self.assertIn("advance", month["dates"]["2026-10-15"])

        current_payload = build_analytics_payload(
            [make_show("8:00 PM", 12, 120)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-15",
            run_id="current-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-15",
            current_payload,
            update_advance_snapshot=False,
            as_of_date="2026-10-15",
        )
        summary = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/fandango_reported/summary"
        ]
        month = database.values[
            "markets/usa/movies/example-film/cumulative/v1/bases/fandango_reported/months/2026-10"
        ]
        self.assertEqual(summary["metrics"]["booked_gross"], 120)
        self.assertEqual(summary["advance_metrics"]["booked_gross"], 100)
        self.assertIn("current", month["dates"]["2026-10-15"])
        self.assertIn("advance", month["dates"]["2026-10-15"])

    def test_empty_snapshot_removes_active_and_advance_pulse_candidates(self):
        database = FakeDatabase()
        first_payload = build_analytics_payload(
            [make_show("8:00 PM", 10, 100)],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="first-run",
        )
        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            first_payload,
            update_advance_snapshot=True,
        )
        empty_payload = build_analytics_payload(
            [],
            market_id="usa",
            movie_id="example-film",
            show_date="2026-10-10",
            run_id="empty-run",
        )

        publish_analytics_payload(
            database,
            "usa",
            "example-film",
            "2026-10-10",
            empty_payload,
            update_advance_snapshot=True,
        )

        self.assertNotIn(
            "markets/usa/pulse/v1/dates/2026-10-10/active/example-film",
            database.values,
        )
        self.assertNotIn(
            "markets/usa/pulse/v1/dates/2026-10-10/advance/example-film",
            database.values,
        )


if __name__ == "__main__":
    unittest.main()
