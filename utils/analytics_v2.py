"""Build compact, market-agnostic RTDB documents from finalized show rows."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone


PAGE_SIZE = 20
DIMENSIONS = (
    "regions",
    "states",
    "cities",
    "chains",
    "platforms",
    "theaters",
    "formats",
    "languages",
    "time_buckets",
)


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _key(value):
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "unknown").strip().lower())
    return text.strip("-_") or "unknown"


def _time_bucket(value):
    text = str(value or "").strip().replace("o'clock", ":00")
    match = re.search(r"(?:T|\s)(\d{1,2}):(\d{2})", text)
    if match:
        hour = int(match.group(1))
        meridiem = re.search(r"\b(AM|PM)\b", text, re.IGNORECASE)
        if meridiem:
            suffix = meridiem.group(1).upper()
            hour = hour % 12 + (12 if suffix == "PM" else 0)
    else:
        match = re.search(r"(\d{1,2})(?::\d{2})?\s*(AM|PM)", text, re.IGNORECASE)
        if not match:
            return "Unknown Time"
        hour = int(match.group(1)) % 12 + (12 if match.group(2).upper() == "PM" else 0)

    if 5 <= hour < 9:
        return "Early Morning (5am-9am)"
    if 9 <= hour < 12:
        return "Morning (9am-12pm)"
    if 12 <= hour < 16:
        return "Afternoon (12pm-4pm)"
    if 16 <= hour < 20:
        return "Evening (4pm-8pm)"
    if 20 <= hour < 24:
        return "Night (8pm-12am)"
    return "Midnight (12am-5am)"


def _show_id(row):
    identity = "|".join(
        str(value or "").strip().lower()
        for value in (
            row.get("t_id"),
            row.get("state"),
            row.get("theater"),
            row.get("time"),
            row.get("format"),
            row.get("language"),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _normalized_format(value):
    text = str(value or "Standard")
    if "D-Box" in text and "Premium" in text:
        return "Premium"
    return text


def _normalize_show(row):
    state = row.get("state") or "Unknown"
    city = row.get("city") or "Unknown"
    theater = row.get("theater") or "Unknown"
    region = row.get("region") or "Unknown"
    capacity = _number(row.get("total"))
    booked = _number(row.get("booked"))
    gross = _number(row.get("gross"))
    time_value = row.get("time") or "Unknown"

    theater_upper = theater.upper()
    if "AMC" in theater_upper:
        chain = "AMC Theatres"
    elif "CINEMARK" in theater_upper or "CENTURY" in theater_upper:
        chain = "Cinemark"
    elif "REGAL" in theater_upper:
        chain = "Regal Cinemas"
    elif "MARCUS" in theater_upper:
        chain = "Marcus Theatres"
    elif "HARKINS" in theater_upper:
        chain = "Harkins Theatres"
    elif "APPLE CINEMAS" in theater_upper:
        chain = "Apple Cinemas"
    else:
        chain = "Other / Independents"

    return {
        "show_id": _show_id(row),
        "theater_id": str(row.get("t_id") or theater),
        "theater_name": str(theater),
        "region_id": _key(region),
        "region_name": str(region),
        "state_id": _key(state),
        "state_name": str(state),
        "city_id": f"{_key(state)}--{_key(city)}",
        "city_name": str(city),
        "chain_id": _key(chain),
        "chain_name": chain,
        "platform_id": "fandango",
        "platform_name": "Fandango",
        "format_id": _key(_normalized_format(row.get("format"))),
        "format_name": _normalized_format(row.get("format")),
        "language_id": _key(row.get("language")),
        "language_name": str(row.get("language") or "Unknown"),
        "time_bucket_id": _key(_time_bucket(time_value)),
        "time_bucket_name": _time_bucket(time_value),
        "local_start_time": str(time_value),
        "capacity_seats": capacity,
        "booked_tickets": booked,
        "booked_gross": gross,
        "occupancy_rate": booked / capacity * 100 if capacity else 0,
        "is_extra": bool(row.get("is_extra") or row.get("t_id") == "EXTRA"),
    }


def _parse_time_minutes(value):
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").upper()
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _match_previous_rows(rows, comparison_rows, time_fallback_minutes):
    unmatched_previous = set(range(len(comparison_rows)))
    previous_by_id = defaultdict(list)
    previous_by_base = defaultdict(list)
    for index, row in enumerate(comparison_rows):
        previous_by_id[row["show_id"]].append(index)
        previous_by_base[(row["theater_id"].casefold(), row["format_id"])].append(index)

    matches = {}
    for index, row in enumerate(rows):
        exact = previous_by_id.get(row["show_id"], [])
        matched_index = next((candidate for candidate in exact if candidate in unmatched_previous), None)
        if matched_index is None and time_fallback_minutes:
            base = (row["theater_id"].casefold(), row["format_id"])
            current_minutes = _parse_time_minutes(row["local_start_time"])
            possible = []
            if current_minutes is not None:
                for candidate in previous_by_base.get(base, []):
                    if candidate not in unmatched_previous:
                        continue
                    prior_minutes = _parse_time_minutes(comparison_rows[candidate]["local_start_time"])
                    if prior_minutes is None:
                        continue
                    difference = abs(current_minutes - prior_minutes)
                    difference = min(difference, 1440 - difference)
                    if difference <= time_fallback_minutes:
                        possible.append((difference, candidate))
            if possible:
                matched_index = min(possible)[1]
        if matched_index is not None:
            unmatched_previous.remove(matched_index)
            matches[index] = comparison_rows[matched_index]
    return matches, [comparison_rows[index] for index in sorted(unmatched_previous)]


def _metrics(rows):
    capacity = sum(row["capacity_seats"] for row in rows)
    booked = sum(row["booked_tickets"] for row in rows)
    gross = sum(row["booked_gross"] for row in rows)
    return {
        "show_count": sum(not row["is_extra"] for row in rows),
        "theater_count": len({row["theater_id"] for row in rows if not row["is_extra"]}),
        "capacity_seats": capacity,
        "booked_tickets": booked,
        "booked_gross": gross,
        "occupancy_rate": booked / capacity * 100 if capacity else 0,
    }


def _aggregate(rows, comparison_rows, time_fallback_minutes=0):
    matched_rows, removed_rows = _match_previous_rows(
        rows, comparison_rows, time_fallback_minutes
    )
    dimensions = {name: {} for name in DIMENSIONS}
    baseline_dimensions = defaultdict(list)
    for row in comparison_rows:
        if row["is_extra"]:
            continue
        for dimension, field in {
            "regions": "region_id",
            "states": "state_id",
            "cities": "city_id",
            "chains": "chain_id",
            "platforms": "platform_id",
            "theaters": "theater_id",
            "formats": "format_id",
            "languages": "language_id",
            "time_buckets": "time_bucket_id",
        }.items():
            baseline_dimensions[(dimension, row[field])].append(row)
    shows = {}
    changes = {
        "added": [],
        "removed": [],
        "tickets_increased": [],
        "tickets_decreased": [],
    }
    grouped_rows = defaultdict(list)

    for row_index, row in enumerate(rows):
        previous = matched_rows.get(row_index)
        row = dict(row)
        row["has_comparison"] = previous is not None
        row["gross_delta"] = row["booked_gross"] - _number(previous and previous.get("booked_gross"))
        row["tickets_delta"] = row["booked_tickets"] - _number(previous and previous.get("booked_tickets"))
        row["capacity_delta"] = row["capacity_seats"] - _number(previous and previous.get("capacity_seats"))
        if not row["is_extra"]:
            shows[row["show_id"]] = row
            if previous is None and comparison_rows:
                changes["added"].append(row)
            elif previous is not None and row["tickets_delta"] > 0:
                changes["tickets_increased"].append(row)
            elif previous is not None and row["tickets_delta"] < 0:
                changes["tickets_decreased"].append(row)

            for dimension in DIMENSIONS:
                entity_id = row[
                    {
                        "regions": "region_id",
                        "states": "state_id",
                        "cities": "city_id",
                        "chains": "chain_id",
                        "platforms": "platform_id",
                        "theaters": "theater_id",
                        "formats": "format_id",
                        "languages": "language_id",
                        "time_buckets": "time_bucket_id",
                    }[dimension]
                ]
                grouped_rows[(dimension, entity_id)].append((row, previous))

    for previous in removed_rows:
        removed = dict(previous)
        removed["gross_delta"] = -_number(previous.get("booked_gross"))
        removed["tickets_delta"] = -_number(previous.get("booked_tickets"))
        changes["removed"].append(removed)

    for (dimension, entity_id), members in grouped_rows.items():
        current_metrics = _metrics([row for row, _ in members])
        baseline_metrics = _metrics(baseline_dimensions[(dimension, entity_id)])
        label_field = {
            "regions": "region_name",
            "states": "state_name",
            "cities": "city_name",
            "chains": "chain_name",
            "platforms": "platform_name",
            "theaters": "theater_name",
            "formats": "format_name",
            "languages": "language_name",
            "time_buckets": "time_bucket_name",
        }[dimension]
        current_metrics.update({
            "id": entity_id,
            "name": members[0][0][label_field],
            "gross_delta": current_metrics["booked_gross"] - baseline_metrics["booked_gross"],
            "tickets_delta": current_metrics["booked_tickets"] - baseline_metrics["booked_tickets"],
            "shows_delta": current_metrics["show_count"] - baseline_metrics["show_count"],
            "occupancy_rate": (
                current_metrics["booked_tickets"] / current_metrics["capacity_seats"] * 100
                if current_metrics["capacity_seats"] else 0
            ),
        })
        dimensions[dimension][entity_id] = current_metrics

    return shows, dimensions, changes


def build_analytics_payload(
    rows,
    market_id,
    movie_id,
    show_date,
    run_id,
    previous_rows=None,
    daily_baseline_rows=None,
    previous_run_id=None,
    previous_run_observed_at=None,
    daily_snapshot_id=None,
    daily_snapshot_observed_at=None,
    currency="USD",
):
    """Build complete summary rollups and paged details for one immutable run."""
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    normalized = [_normalize_show(row) for row in rows]
    previous_normalized = [_normalize_show(row) for row in (previous_rows or [])]
    daily_normalized = [_normalize_show(row) for row in (daily_baseline_rows or [])]

    current_shows, dimensions, previous_changes = _aggregate(
        normalized, previous_normalized, time_fallback_minutes=60
    )
    daily_shows, daily_dimensions, daily_changes = _aggregate(
        normalized, daily_normalized, time_fallback_minutes=60
    )
    metrics = _metrics(normalized)
    previous_metrics = _metrics(previous_normalized)
    daily_metrics = _metrics(daily_normalized)
    previous_run_delta = {
        "booked_gross": metrics["booked_gross"] - previous_metrics["booked_gross"],
        "booked_tickets": metrics["booked_tickets"] - previous_metrics["booked_tickets"],
        "capacity_seats": metrics["capacity_seats"] - previous_metrics["capacity_seats"],
        "show_count": metrics["show_count"] - previous_metrics["show_count"],
        "theater_count": metrics["theater_count"] - previous_metrics["theater_count"],
    }
    daily_snapshot_delta = {
        "booked_gross": metrics["booked_gross"] - daily_metrics["booked_gross"],
        "booked_tickets": metrics["booked_tickets"] - daily_metrics["booked_tickets"],
        "capacity_seats": metrics["capacity_seats"] - daily_metrics["capacity_seats"],
        "show_count": metrics["show_count"] - daily_metrics["show_count"],
        "theater_count": metrics["theater_count"] - daily_metrics["theater_count"],
    }

    tables = {}
    for dimension, entities in dimensions.items():
        daily_entities = daily_dimensions[dimension]
        rows_by_id = []
        for entity_id, item in entities.items():
            daily_item = daily_entities.get(entity_id, {})
            rows_by_id.append({
                **item,
                "gross_delta_previous_run": item["gross_delta"],
                "tickets_delta_previous_run": item["tickets_delta"],
                "shows_delta_previous_run": item["shows_delta"],
                "gross_delta_daily_snapshot": daily_item.get("gross_delta", item["booked_gross"]),
                "tickets_delta_daily_snapshot": daily_item.get("tickets_delta", item["booked_tickets"]),
                "shows_delta_daily_snapshot": daily_item.get("shows_delta", item["show_count"]),
            })
        tables[dimension] = sorted(
            rows_by_id,
            key=lambda item: (-item["booked_gross"], item["id"]),
        )
    raw_details = {
        normalized_row["show_id"]: {
            key: raw_row[key]
            for key in (
                "state", "city", "region", "t_id", "theater", "format",
                "language", "time", "status", "price_str", "total", "booked",
                "gross", "is_extra",
            )
            if key in raw_row
        }
        for raw_row, normalized_row in zip(rows, normalized)
        if not normalized_row["is_extra"]
    }
    details = {}
    for show_id, current_row in current_shows.items():
        daily_row = daily_shows.get(show_id)
        details[show_id] = {
            **raw_details.get(show_id, {}),
            "gross_delta_previous_run": current_row["gross_delta"],
            "tickets_delta_previous_run": current_row["tickets_delta"],
            "gross_delta_daily_snapshot": (
                daily_row["gross_delta"] if daily_row else current_row["booked_gross"]
            ),
            "tickets_delta_daily_snapshot": (
                daily_row["tickets_delta"] if daily_row else current_row["booked_tickets"]
            ),
        }

    return {
        "current": {
            "schema_version": 3,
            "market_id": market_id,
            "movie_id": movie_id,
            "show_date": show_date,
            "run_id": run_id,
            "observed_at": observed_at,
            "currency": currency,
            "metrics": metrics,
            "tables": tables,
            "growth": {
                "previous_run": {
                    "available": bool(previous_rows),
                    "base_run_id": previous_run_id,
                    "observed_at": previous_run_observed_at,
                    "metrics": previous_run_delta,
                },
                "daily_snapshot": {
                    "available": bool(daily_baseline_rows),
                    "base_snapshot_id": daily_snapshot_id,
                    "observed_at": daily_snapshot_observed_at,
                    "metrics": daily_snapshot_delta,
                },
            },
            "comparison": {
                "previous_run": {
                    "base_run_id": previous_run_id,
                    "available": bool(previous_rows),
                    "observed_at": previous_run_observed_at,
                    "change_counts": {key: len(value) for key, value in previous_changes.items()},
                },
                "daily_snapshot": {
                    "base_snapshot_id": daily_snapshot_id,
                    "available": bool(daily_baseline_rows),
                    "observed_at": daily_snapshot_observed_at,
                    "change_counts": {key: len(value) for key, value in daily_changes.items()},
                },
            },
        },
        "details": details,
        "change_pages": {
            "previous_run": previous_changes,
            "daily_snapshot": daily_changes,
        },
        "comparison_baseline": {
            "shows": {
                show_id: {
                    key: raw_row[key]
                    for key in (
                        "state", "t_id", "theater", "format", "language", "time",
                        "status", "price_str", "total", "booked", "gross", "is_extra",
                    )
                    if key in raw_row
                }
                for raw_row, normalized_row in zip(rows, normalized)
                if not normalized_row["is_extra"]
                for show_id in (normalized_row["show_id"],)
            },
            "extras": [
                {
                    key: raw_row[key]
                    for key in (
                        "state", "t_id", "theater", "format", "language", "time",
                        "status", "price_str", "total", "booked", "gross", "is_extra",
                    )
                    if key in raw_row
                }
                for raw_row, normalized_row in zip(rows, normalized)
                if normalized_row["is_extra"]
            ],
        },
        "history_entry": {
            "timestamp": observed_at,
            "total_gross": metrics["booked_gross"],
            "booked_tickets": metrics["booked_tickets"],
            "occupancy": metrics["occupancy_rate"],
            "shows": metrics["show_count"],
        },
    }


def publish_analytics_payload(
    db,
    market_id,
    movie_id,
    show_date,
    payload,
    update_daily_baseline=False,
    update_advance_snapshot=False,
):
    """Stage summaries and independently fetchable pages before publishing a pointer."""
    root = f"markets/{market_id}/movies/{movie_id}/dates/{show_date}"
    run_id = payload["current"]["run_id"]
    db.reference(f"markets/{market_id}/movie_index/{movie_id}").update({
        "movie_id": movie_id,
        "name": str(movie_id).replace("-", " ").replace("_", " ").title(),
    })
    db.reference(
        f"markets/{market_id}/movies/{movie_id}/date_index/{show_date}"
    ).set(True)
    published_ref = db.reference(f"{root}/published")
    previous = published_ref.get() or {}
    previous_run_id = previous.get("active_run_id")
    previous_advance_run_id = previous.get("advance_run_id")
    run_root = f"{root}/analytics_v2/runs/{run_id}"
    db.reference(f"{run_root}/summary").set(payload["current"])

    shows = payload["details"]
    ordered_show_ids = sorted(
        shows,
        key=lambda show_id: (-_number(shows[show_id].get("gross")), show_id),
    )
    detail_root = f"{root}/movie_details/runs/{run_id}/pages"
    for offset in range(0, len(ordered_show_ids), PAGE_SIZE):
        page_number = offset // PAGE_SIZE + 1
        page_ids = ordered_show_ids[offset:offset + PAGE_SIZE]
        db.reference(f"{detail_root}/{page_number:04d}").set({
            show_id: shows[show_id] for show_id in page_ids
        })

    for comparison_name, changes in payload["change_pages"].items():
        for change_type, changed_rows in changes.items():
            ordered_changes = sorted(
                changed_rows,
                key=lambda row: (
                    -_number(row.get("gross", row.get("booked_gross"))),
                    row.get("show_id", ""),
                ),
            )
            for offset in range(0, len(ordered_changes), PAGE_SIZE):
                page_number = offset // PAGE_SIZE + 1
                db.reference(
                    f"{run_root}/pages/comparisons/{comparison_name}/{change_type}/{page_number:04d}"
                ).set(ordered_changes[offset:offset + PAGE_SIZE])

    if update_daily_baseline:
        db.reference(f"{root}/comparison_baselines/daily").set({
            "run_id": run_id,
            "observed_at": payload["current"]["observed_at"],
            **payload["comparison_baseline"],
        })

    db.reference(f"{root}/history/{run_id}").set(payload["history_entry"])
    advance_run_id = run_id if update_advance_snapshot else previous_advance_run_id
    detail_count = len(shows)
    detail_page_count = (detail_count + PAGE_SIZE - 1) // PAGE_SIZE
    previous_detail_count = previous.get("advance_detail_count")
    previous_detail_page_count = previous.get("advance_detail_page_count")
    if previous_advance_run_id and previous_detail_count is None:
        if previous_advance_run_id == previous_run_id:
            previous_detail_count = previous.get("detail_count", 0)
            previous_detail_page_count = previous.get("detail_page_count")
        else:
            advance_summary = db.reference(
                f"{root}/analytics_v2/runs/{previous_advance_run_id}/summary"
            ).get() or {}
            previous_detail_count = (advance_summary.get("metrics") or {}).get(
                "show_count", 0
            )
    previous_detail_count = int(previous_detail_count or 0)
    previous_detail_page_count = int(
        previous_detail_page_count
        if previous_detail_page_count is not None
        else (previous_detail_count + PAGE_SIZE - 1) // PAGE_SIZE
    )
    advance_detail_count = detail_count if update_advance_snapshot else previous_detail_count
    advance_detail_page_count = (
        detail_page_count if update_advance_snapshot else previous_detail_page_count
    )
    published_value = {
        "schema_version": 2,
        "active_run_id": run_id,
        "advance_run_id": advance_run_id,
        "observed_at": payload["current"]["observed_at"],
        "details_available": bool(shows),
        "detail_count": detail_count,
        "detail_page_size": PAGE_SIZE,
        "detail_page_count": detail_page_count,
        "advance_details_available": bool(advance_run_id and advance_detail_count),
        "active_detail_count": detail_count,
        "active_detail_page_count": detail_page_count,
        "active_details_available": bool(shows),
        "advance_detail_count": advance_detail_count,
        "advance_detail_page_count": advance_detail_page_count,
    }
    published_ref.set(published_value)

    for old_run_id in {previous_run_id, previous_advance_run_id} - {None, run_id, advance_run_id}:
        db.reference(f"{root}/movie_details/runs/{old_run_id}").delete()
        db.reference(f"{root}/analytics_v2/runs/{old_run_id}").delete()
    return previous
