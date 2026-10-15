"""Build portal views using PORTAL_MARKETS and INR_TO_USD environment variables."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

import pytz


LOGGER = logging.getLogger("build_portal_views")
DEFAULT_MARKETS = ("india", "usa")
DEFAULT_INR_TO_USD = 0.0118
PREFERRED_GROSS_BASIS = {
    "india": "bms",
    "usa": "fandango_reported",
}
MARKET_CURRENCY = {
    "india": "INR",
    "usa": "USD",
}
MARKET_TIMEZONES = {
    "india": "Asia/Kolkata",
    "usa": "America/Chicago",
}


def _market_today(market_id: str, now: datetime | None = None) -> date:
    timezone_name = MARKET_TIMEZONES.get(market_id.lower(), "UTC")
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        raise ValueError("The reference datetime must be timezone-aware.")
    return reference_time.astimezone(pytz.timezone(timezone_name)).date()


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    return text.strip("-")


def _title_from_id(movie_id: str) -> str:
    return re.sub(r"[-_]+", " ", movie_id).strip().title()


def _date_value(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _occupancy(metrics: dict) -> float:
    capacity = _number(metrics.get("capacity_seats"))
    tickets = _number(metrics.get("booked_tickets"))
    if capacity > 0:
        return tickets / capacity
    occupancy_rate = _number(metrics.get("occupancy_rate"))
    return occupancy_rate / 100 if occupancy_rate > 1 else occupancy_rate


def _summary_reference(db: Any, market_id: str, movie_id: str, show_date: str, run_id: str):
    return db.reference(
        f"markets/{market_id}/movies/{movie_id}/dates/{show_date}/"
        f"analytics_v2/runs/{run_id}/summary"
    )


def _choose_basis(
    bases: dict,
    market_id: str,
    metadata: dict,
    movie_index: dict,
) -> tuple[str, dict]:
    if not bases:
        return "", {}

    requested = (
        metadata.get("gross_basis")
        or movie_index.get("gross_basis")
        or PREFERRED_GROSS_BASIS.get(market_id.lower())
    )
    if requested in bases:
        return requested, _as_dict(bases[requested])
    if len(bases) == 1:
        basis = next(iter(bases))
        return basis, _as_dict(bases[basis])

    raise ValueError(
        f"Movie {market_id}/{movie_index.get('movie_id', '')} has multiple "
        "cumulative gross bases and no configured preferred basis."
    )


def _read_date_summaries(
    db: Any,
    market_id: str,
    movie_id: str,
    rollup_dates: dict,
    date_index: dict,
    as_of_date: date,
) -> tuple[dict[str, dict], dict[str, dict], set[str]]:
    current_summaries = {}
    advance_summaries = {}
    estimated_current_dates = set()

    for show_date in sorted(rollup_dates):
        day = _date_value(show_date)
        if day is None:
            LOGGER.warning("Skipping invalid show date %s/%s/%s.", market_id, movie_id, show_date)
            continue

        rollup = _as_dict(rollup_dates[show_date])
        current_contribution = _as_dict(rollup.get("current"))
        advance_contribution = _as_dict(rollup.get("advance"))
        published = _as_dict(
            db.reference(
                f"markets/{market_id}/movies/{movie_id}/dates/{show_date}/published"
            ).get()
        )
        availability = _as_dict(date_index.get(show_date))
        has_active_run = bool(
            availability.get("has_current") or published.get("active_run_id")
        )
        use_advance_for_current = (
            day <= as_of_date
            and not has_active_run
            and _has_authoritative_sales(advance_contribution)
        )

        if (
            day <= as_of_date
            and (
                use_advance_for_current
                or (
                    _scope_available(date_index, show_date, "current", day, as_of_date)
                    and isinstance(rollup.get("current"), dict)
                )
            )
        ):
            run_id = published.get(
                "advance_run_id" if use_advance_for_current else "active_run_id"
            )
            if run_id:
                summary = _as_dict(
                    _summary_reference(db, market_id, movie_id, show_date, str(run_id)).get()
                )
                if summary:
                    current_summaries[show_date] = summary
                    if use_advance_for_current:
                        estimated_current_dates.add(show_date)
                else:
                    LOGGER.warning(
                        "Missing %s analytics summary for %s/%s/%s run %s.",
                        "advance" if use_advance_for_current else "active",
                        market_id,
                        movie_id,
                        show_date,
                        run_id,
                    )
            else:
                LOGGER.warning(
                    "Missing %s run pointer for %s/%s/%s.",
                    "advance" if use_advance_for_current else "active",
                    market_id,
                    movie_id,
                    show_date,
                )

        if (
            _scope_available(date_index, show_date, "advance", day, as_of_date)
            and isinstance(rollup.get("advance"), dict)
        ):
            run_id = published.get("advance_run_id")
            if run_id:
                summary = _as_dict(
                    _summary_reference(db, market_id, movie_id, show_date, str(run_id)).get()
                )
                if summary:
                    advance_summaries[show_date] = summary
                else:
                    LOGGER.warning(
                        "Missing advance analytics summary for %s/%s/%s run %s.",
                        market_id,
                        movie_id,
                        show_date,
                        run_id,
                    )
            else:
                LOGGER.warning(
                    "Missing advance run pointer for %s/%s/%s.",
                    market_id,
                    movie_id,
                    show_date,
                )

    return current_summaries, advance_summaries, estimated_current_dates


def _delta(metrics: dict, public_key: str, legacy_key: str) -> float:
    if public_key in metrics:
        return _number(metrics.get(public_key))
    previous = _as_dict(metrics.get("previous_run_delta"))
    return _number(previous.get(legacy_key))


def _scope_totals(
    rollup_dates: dict,
    date_index: dict,
    scope: str,
    as_of_date: date,
) -> dict:
    totals = {"gross": 0.0, "tickets": 0, "shows": 0}
    for show_date, entry in rollup_dates.items():
        day = _date_value(show_date)
        if day is None:
            continue
        entry = _as_dict(entry)
        if scope == "current":
            if day > as_of_date:
                continue
            if not _scope_available(date_index, show_date, scope, day, as_of_date):
                continue
            contribution = _current_contribution(entry, date_index, show_date)
        else:
            if day <= as_of_date:
                continue
            if not _scope_available(date_index, show_date, scope, day, as_of_date):
                continue
            contribution = _as_dict(entry.get(scope))
        if not contribution:
            continue
        totals["gross"] += _number(contribution.get("booked_gross"))
        totals["tickets"] += _integer(contribution.get("booked_tickets"))
        totals["shows"] += _integer(contribution.get("show_count"))
    return totals


def _has_authoritative_sales(contribution: Any) -> bool:
    metrics = _as_dict(contribution)
    return (
        _number(metrics.get("booked_gross")) > 0
        or _number(metrics.get("booked_tickets")) > 0
    )


def _lifecycle_rollup(
    rollup_dates: dict,
    as_of_date: date,
    date_index: dict | None = None,
) -> dict:
    totals = {
        "total_gross_so_far": 0.0,
        "total_tickets_so_far": 0,
        "total_shows_so_far": 0,
        "advance_sales_from_tomorrow": 0.0,
        "advance_tickets_from_tomorrow": 0,
        "advance_shows_from_tomorrow": 0,
    }
    valid_dates = []

    for show_date, raw_entry in rollup_dates.items():
        day = _date_value(show_date)
        if day is None:
            continue
        entry = _as_dict(raw_entry)
        current = _as_dict(entry.get("current"))
        advance = _as_dict(entry.get("advance"))
        current_valid = _has_authoritative_sales(current)
        advance_valid = _has_authoritative_sales(advance)
        if current_valid or advance_valid:
            valid_dates.append(day)

        if day <= as_of_date:
            if date_index is None:
                selected = current if current_valid else (advance if advance_valid else {})
            elif _scope_available(date_index, show_date, "current", day, as_of_date):
                selected = _current_contribution(entry, date_index, show_date)
            else:
                selected = {}
            total_prefix = "total"
        else:
            selected = advance if advance_valid else {}
            total_prefix = "advance"

        if not selected:
            continue
        totals[f"{total_prefix}_gross_so_far" if total_prefix == "total" else "advance_sales_from_tomorrow"] += _number(
            selected.get("booked_gross")
        )
        totals[
            "total_tickets_so_far"
            if total_prefix == "total"
            else "advance_tickets_from_tomorrow"
        ] += _integer(selected.get("booked_tickets"))
        totals[
            "total_shows_so_far"
            if total_prefix == "total"
            else "advance_shows_from_tomorrow"
        ] += _integer(selected.get("show_count"))

    if not valid_dates:
        status = "unknown"
        first_valid_date = ""
        last_valid_date = ""
    else:
        first_day = min(valid_dates)
        last_day = max(valid_dates)
        first_valid_date = first_day.isoformat()
        last_valid_date = last_day.isoformat()
        if first_day > as_of_date:
            status = "coming_soon"
        elif last_day < as_of_date:
            status = "archived"
        else:
            status = "playing_now"

    return {
        **totals,
        "status": status,
        "valid_dates": sorted(day.isoformat() for day in set(valid_dates)),
        "first_valid_show_date": first_valid_date,
        "last_valid_show_date": last_valid_date,
    }


def _occupancy_rollup(summaries: dict[str, dict]) -> float:
    total_tickets = 0.0
    total_capacity = 0.0
    fallback_occupancies = []
    for summary in summaries.values():
        metrics = _as_dict(summary.get("metrics"))
        capacity = _number(metrics.get("capacity_seats"))
        if capacity > 0:
            total_tickets += _number(metrics.get("booked_tickets"))
            total_capacity += capacity
        else:
            fallback_occupancies.append(_occupancy(metrics))
    if total_capacity:
        return total_tickets / total_capacity
    if fallback_occupancies:
        return sum(fallback_occupancies) / len(fallback_occupancies)
    return 0.0


def _latest_metrics(summaries: dict[str, dict]) -> dict:
    if not summaries:
        return {}
    latest_date = max(summaries)
    summary = _as_dict(summaries[latest_date])
    metrics = dict(_as_dict(summary.get("metrics")))
    previous_run = _as_dict(_as_dict(summary.get("growth")).get("previous_run"))
    previous_metrics = _as_dict(previous_run.get("metrics"))
    if previous_metrics:
        metrics["previous_run_delta"] = previous_metrics
    return metrics


def _scope_available(
    date_index: dict,
    show_date: str,
    scope: str,
    show_day: date,
    as_of_date: date,
) -> bool:
    entry = _as_dict(date_index.get(show_date))
    if scope == "current" and show_day > as_of_date:
        return False
    key = f"has_{scope}"
    if key in entry:
        if scope == "current":
            return bool(entry[key] or entry.get("has_advance"))
        return bool(entry[key])
    return show_day <= as_of_date if scope == "current" else show_day > as_of_date


def _current_contribution(entry: dict, date_index: dict, show_date: str) -> dict:
    current = _as_dict(entry.get("current"))
    availability = _as_dict(date_index.get(show_date))
    has_active_run = availability.get("has_current")
    if has_active_run is None:
        has_active_run = bool(current)
    return current if has_active_run else _as_dict(entry.get("advance"))


def _build_trajectory(
    rollup_dates: dict,
    summaries: dict[str, dict],
    date_index: dict,
    scope: str,
    as_of_date: date,
) -> list[dict]:
    trajectory = []
    cumulative_gross = 0.0
    for show_date in sorted(rollup_dates):
        day = _date_value(show_date)
        if day is None or (scope == "current" and day > as_of_date):
            continue
        if not _scope_available(date_index, show_date, scope, day, as_of_date):
            continue
        rollup = _as_dict(rollup_dates[show_date])
        contribution = (
            _current_contribution(rollup, date_index, show_date)
            if scope == "current"
            else _as_dict(rollup.get(scope))
        )
        if not contribution:
            continue

        daily_gross = _number(contribution.get("booked_gross"))
        cumulative_gross += daily_gross
        metrics = _as_dict(_as_dict(summaries.get(show_date)).get("metrics"))
        point = {
            "date": show_date,
            "daily_gross": daily_gross,
            "cumulative_gross": cumulative_gross,
            "tickets": _integer(contribution.get("booked_tickets")),
            "shows": _integer(contribution.get("show_count")),
            "capacity_seats": _integer(metrics.get("capacity_seats")),
            "occupancy": _occupancy(metrics),
        }
        if scope == "advance":
            point["advance_gross"] = daily_gross
        trajectory.append(point)
    return trajectory


def _movie_documents(
    db: Any,
    market_id: str,
    movie_id: str,
    movie_index: dict,
    as_of_date: date,
    updated_at: str,
) -> tuple[dict, dict]:
    root = f"markets/{market_id}/movies/{movie_id}"
    metadata = _as_dict(db.reference(f"{root}/meta").get())
    raw_date_index = _as_dict(db.reference(f"{root}/date_index").get())
    source_date_index = raw_date_index
    date_index = {}
    for show_date, raw_availability in raw_date_index.items():
        day = _date_value(show_date)
        availability = _as_dict(raw_availability)
        if day is None:
            date_index[show_date] = availability
            continue
        date_index[show_date] = {
            **availability,
            "has_current": (
                day <= as_of_date and bool(
                    availability.get("has_current") or availability.get("has_advance")
                )
            ),
            "has_advance": (
                bool(availability.get("has_advance"))
            ),
        }
    cumulative = _as_dict(db.reference(f"{root}/cumulative/v1/bases").get())
    basis, basis_data = _choose_basis(cumulative, market_id, metadata, movie_index)
    months = _as_dict(basis_data.get("months"))
    if not months:
        LOGGER.warning("No cumulative monthly rollups for %s/%s.", market_id, movie_id)

    rollup_dates = {}
    for month in months.values():
        rollup_dates.update(_as_dict(_as_dict(month).get("dates")))

    lifecycle = _lifecycle_rollup(rollup_dates, as_of_date, source_date_index)
    current_totals = _scope_totals(
        rollup_dates,
        source_date_index,
        "current",
        as_of_date,
    )
    advance_totals = _scope_totals(
        rollup_dates, source_date_index, "advance", as_of_date
    )
    current_summaries, advance_summaries, estimated_current_dates = _read_date_summaries(
        db,
        market_id,
        movie_id,
        rollup_dates,
        source_date_index,
        as_of_date,
    )
    future_advance_summaries = {
        show_date: summary
        for show_date, summary in advance_summaries.items()
        if (_date_value(show_date) or as_of_date) > as_of_date
    }

    title = str(
        metadata.get("title")
        or metadata.get("name")
        or movie_index.get("title")
        or movie_index.get("name")
        or _title_from_id(movie_id)
    )
    global_slug = str(
        metadata.get("global_slug")
        or movie_index.get("global_slug")
        or _slug(title)
        or _slug(movie_id)
    )
    meta = {
        "title": title,
        "global_slug": global_slug,
        "poster_url": metadata.get("poster_url") or movie_index.get("poster_url") or "",
        "cover_url": metadata.get("cover_url") or movie_index.get("cover_url") or "",
        "release_date": metadata.get("release_date") or movie_index.get("release_date") or "",
        "last_tracked_date": lifecycle["last_valid_show_date"],
    }
    currency = str(
        metadata.get("currency")
        or basis_data.get("currency")
        or movie_index.get("currency")
        or MARKET_CURRENCY.get(market_id.lower(), "USD")
    )
    latest_current = _latest_metrics(current_summaries)
    latest_advance = _latest_metrics(future_advance_summaries)
    current = {
        "total_gross": current_totals["gross"],
        "total_tickets": current_totals["tickets"],
        "total_shows": current_totals["shows"],
        "avg_occupancy": _occupancy_rollup(current_summaries),
        "avg_occupancy_is_estimate": bool(estimated_current_dates),
        "fast_fillings": _integer(latest_current.get("fast_fillings")),
        "sold_outs": _integer(latest_current.get("sold_outs")),
        "gross_delta": _delta(latest_current, "gross_delta", "booked_gross"),
        "tickets_delta": _delta(latest_current, "tickets_delta", "booked_tickets"),
        "shows_delta": _delta(latest_current, "shows_delta", "show_count"),
        "venues_delta": _delta(latest_current, "venues_delta", "theater_count"),
    }
    advance = {
        "advance_gross": advance_totals["gross"],
        "advance_tickets": advance_totals["tickets"],
        "advance_shows": advance_totals["shows"],
        "avg_occupancy": _occupancy_rollup(future_advance_summaries),
        "fast_fillings": _integer(latest_advance.get("fast_fillings")),
        "sold_outs": _integer(latest_advance.get("sold_outs")),
        "gross_delta": _delta(latest_advance, "gross_delta", "booked_gross"),
        "tickets_delta": _delta(latest_advance, "tickets_delta", "booked_tickets"),
        "shows_delta": _delta(latest_advance, "shows_delta", "show_count"),
        "venues_delta": _delta(latest_advance, "venues_delta", "theater_count"),
    }

    summary_view = {
        "schema_version": 2,
        "market_id": market_id,
        "movie_id": movie_id,
        "currency": currency,
        "updated_at": updated_at,
        "status": lifecycle["status"],
        "as_of_date": as_of_date.isoformat(),
        "first_valid_show_date": lifecycle["first_valid_show_date"],
        "last_valid_show_date": lifecycle["last_valid_show_date"],
        "total_gross_so_far": lifecycle["total_gross_so_far"],
        "total_tickets_so_far": lifecycle["total_tickets_so_far"],
        "total_shows_so_far": lifecycle["total_shows_so_far"],
        "advance_sales_from_tomorrow": lifecycle["advance_sales_from_tomorrow"],
        "advance_tickets_from_tomorrow": lifecycle["advance_tickets_from_tomorrow"],
        "advance_shows_from_tomorrow": lifecycle["advance_shows_from_tomorrow"],
        "meta": meta,
        "date_index": date_index,
        "lifetime_kpis": {
            "total_gross": current_totals["gross"],
            "total_tickets": current_totals["tickets"],
            "total_shows": current_totals["shows"],
            "avg_occupancy": _occupancy_rollup(current_summaries),
            "advance_gross": advance_totals["gross"],
            "advance_tickets": advance_totals["tickets"],
            "advance_shows": advance_totals["shows"],
            "advance_avg_occupancy": _occupancy_rollup(future_advance_summaries),
        },
        "daily_trajectory_current": _build_trajectory(
            rollup_dates, current_summaries, source_date_index, "current", as_of_date
        ),
        "daily_trajectory_advance": _build_trajectory(
            rollup_dates, advance_summaries, source_date_index, "advance", as_of_date
        ),
        "gross_basis": basis,
    }
    catalog_item = {
        "movie_id": movie_id,
        **meta,
        "status": lifecycle["status"],
        "as_of_date": as_of_date.isoformat(),
        "first_valid_show_date": lifecycle["first_valid_show_date"],
        "total_gross_so_far": lifecycle["total_gross_so_far"],
        "total_tickets_so_far": lifecycle["total_tickets_so_far"],
        "total_shows_so_far": lifecycle["total_shows_so_far"],
        "advance_sales_from_tomorrow": lifecycle["advance_sales_from_tomorrow"],
        "advance_tickets_from_tomorrow": lifecycle["advance_tickets_from_tomorrow"],
        "advance_shows_from_tomorrow": lifecycle["advance_shows_from_tomorrow"],
        "currency": currency,
        "current": current,
        "advance": advance,
    }
    return summary_view, catalog_item


def _ranked(items: list[dict], sales_scope: str) -> list[dict]:
    gross_key = "total_gross" if sales_scope == "current" else "advance_gross"
    return sorted(
        (item for item in items if _number(_as_dict(item.get(sales_scope)).get(gross_key)) > 0),
        key=lambda item: (
            -_number(_as_dict(item.get(sales_scope)).get(gross_key)),
            str(item.get("movie_id", "")),
        ),
    )[:10]


def _ranked_lifecycle(
    items: list[dict],
    status: str,
    gross_key: str,
    tickets_key: str,
    id_key: str,
) -> list[dict]:
    eligible = [
        item
        for item in items
        if item.get("status") == status
        and (
            _number(item.get(gross_key)) > 0
            or _integer(item.get(tickets_key)) > 0
        )
    ]
    return sorted(
        eligible,
        key=lambda item: (
            -_number(item.get(gross_key)),
            -_integer(item.get(tickets_key)),
            str(item.get(id_key, "")),
        ),
    )[:10]


def _build_market_portal(
    db: Any,
    market_id: str,
    as_of_date: date,
    updated_at: str,
) -> dict:
    market_root = f"markets/{market_id}"
    movie_index = _as_dict(db.reference(f"{market_root}/movie_index").get())
    old_portal = _as_dict(db.reference(f"{market_root}/portal_view").get())
    old_catalog = {
        str(item.get("movie_id")): item
        for item in old_portal.get("movie_catalog", [])
        if isinstance(item, dict) and item.get("movie_id")
    }
    catalog = []

    for movie_id, raw_index_entry in sorted(movie_index.items()):
        movie_index_entry = _as_dict(raw_index_entry)
        try:
            summary_view, catalog_item = _movie_documents(
                db,
                market_id,
                str(movie_id),
                movie_index_entry,
                as_of_date,
                updated_at,
            )
            db.reference(
                f"{market_root}/movies/{movie_id}/summary_view"
            ).set(summary_view)
        except Exception:
            LOGGER.exception("Could not build portal view for %s/%s.", market_id, movie_id)
            catalog_item = old_catalog.get(str(movie_id))
        if catalog_item:
            catalog.append(catalog_item)

    currencies = {
        str(item.get("currency"))
        for item in catalog
        if item.get("currency")
    }
    currency = (
        next(iter(currencies))
        if len(currencies) == 1
        else MARKET_CURRENCY.get(market_id.lower(), "USD")
    )
    return {
        "schema_version": 2,
        "market_id": market_id,
        "currency": currency,
        "updated_at": updated_at,
        "movie_catalog": catalog,
        "top10_playing_now": _ranked_lifecycle(
            catalog,
            "playing_now",
            "total_gross_so_far",
            "total_tickets_so_far",
            "movie_id",
        ),
        "top10_coming_soon": _ranked_lifecycle(
            catalog,
            "coming_soon",
            "advance_sales_from_tomorrow",
            "advance_tickets_from_tomorrow",
            "movie_id",
        ),
        "top10_current": _ranked(catalog, "current"),
        "top10_advance": _ranked(catalog, "advance"),
    }


def _usd_amount(amount: float, currency: str, inr_to_usd: float) -> float:
    currency_code = currency.upper()
    if currency_code == "USD":
        return amount
    if currency_code == "INR":
        return amount * inr_to_usd
    raise ValueError(f"No configured USD exchange rate for currency {currency_code!r}.")


def _inr_amount(amount: float, currency: str, inr_to_usd: float) -> float:
    currency_code = currency.upper()
    if currency_code == "INR":
        return amount
    if currency_code == "USD":
        return amount / inr_to_usd
    raise ValueError(f"No configured INR exchange rate for currency {currency_code!r}.")


def _build_global_portal(
    market_portals: dict[str, dict],
    updated_at: str,
    inr_to_usd: float,
    previous_global_catalog: list[dict] | None = None,
) -> dict:
    reconciled = {}
    artwork_sources = defaultdict(list)
    previous_catalog = {
        str(item.get("global_slug")): item
        for item in (previous_global_catalog or [])
        if isinstance(item, dict) and item.get("global_slug")
    }
    primary_market = next(iter(market_portals), None)
    for market_id, portal in market_portals.items():
        for item in portal.get("movie_catalog", []):
            global_slug = str(item.get("global_slug") or "")
            if not global_slug:
                LOGGER.warning("Skipping %s movie without global_slug.", market_id)
                continue
            if global_slug not in reconciled:
                reconciled[global_slug] = {
                    "global_slug": global_slug,
                    "title": item.get("title") or "",
                    "last_tracked_date": item.get("last_tracked_date") or "",
                    "global_gross_usd": 0.0,
                    "global_gross_inr": 0.0,
                    "global_advance_usd": 0.0,
                    "global_advance_inr": 0.0,
                    "global_tickets": 0,
                    "global_advance_tickets": 0,
                    "global_total_gross_so_far_usd": 0.0,
                    "global_total_gross_so_far_inr": 0.0,
                    "global_advance_sales_from_tomorrow_usd": 0.0,
                    "global_advance_sales_from_tomorrow_inr": 0.0,
                    "global_tickets_so_far": 0,
                    "global_advance_tickets_from_tomorrow": 0,
                    "global_shows_so_far": 0,
                    "global_advance_shows_from_tomorrow": 0,
                    "status": "unknown",
                    "market_statuses": {},
                    "market_metrics": {},
                    "markets": [],
                }
            entry = reconciled[global_slug]
            artwork_sources[global_slug].append((market_id, item))
            current = _as_dict(item.get("current"))
            advance = _as_dict(item.get("advance"))
            currency = str(item.get("currency") or portal.get("currency") or "USD")
            market_metrics = entry["market_metrics"].setdefault(
                market_id,
                {
                    "currency": currency,
                    "current": {"gross": 0.0, "tickets": 0},
                    "advance": {"gross": 0.0, "tickets": 0},
                    "status": "unknown",
                    "total_gross_so_far": 0.0,
                    "tickets_so_far": 0,
                    "advance_sales_from_tomorrow": 0.0,
                    "advance_tickets_from_tomorrow": 0,
                    "total_shows_so_far": 0,
                    "advance_shows_from_tomorrow": 0,
                },
            )
            market_metrics["status"] = item.get("status", "unknown")
            market_metrics["total_gross_so_far"] += _number(
                item.get("total_gross_so_far")
            )
            market_metrics["tickets_so_far"] += _integer(
                item.get("total_tickets_so_far")
            )
            market_metrics["advance_sales_from_tomorrow"] += _number(
                item.get("advance_sales_from_tomorrow")
            )
            market_metrics["advance_tickets_from_tomorrow"] += _integer(
                item.get("advance_tickets_from_tomorrow")
            )
            market_metrics["total_shows_so_far"] += _integer(
                item.get("total_shows_so_far")
            )
            market_metrics["advance_shows_from_tomorrow"] += _integer(
                item.get("advance_shows_from_tomorrow")
            )
            entry["market_statuses"][market_id] = item.get("status", "unknown")
            status_priority = {
                "playing_now": 3,
                "coming_soon": 2,
                "archived": 1,
                "unknown": 0,
            }
            if status_priority.get(item.get("status"), 0) > status_priority.get(
                entry["status"], 0
            ):
                entry["status"] = item.get("status", "unknown")
            market_metrics["current"]["gross"] += _number(current.get("total_gross"))
            market_metrics["current"]["tickets"] += _integer(current.get("total_tickets"))
            market_metrics["advance"]["gross"] += _number(advance.get("advance_gross"))
            market_metrics["advance"]["tickets"] += _integer(advance.get("advance_tickets"))
            try:
                current_gross = _number(current.get("total_gross"))
                advance_gross = _number(advance.get("advance_gross"))
                total_gross_so_far = _number(item.get("total_gross_so_far"))
                advance_from_tomorrow = _number(
                    item.get("advance_sales_from_tomorrow")
                )
                entry["global_gross_usd"] += _usd_amount(current_gross, currency, inr_to_usd)
                entry["global_gross_inr"] += _inr_amount(current_gross, currency, inr_to_usd)
                entry["global_advance_usd"] += _usd_amount(advance_gross, currency, inr_to_usd)
                entry["global_advance_inr"] += _inr_amount(advance_gross, currency, inr_to_usd)
                entry["global_total_gross_so_far_usd"] += _usd_amount(
                    total_gross_so_far, currency, inr_to_usd
                )
                entry["global_total_gross_so_far_inr"] += _inr_amount(
                    total_gross_so_far, currency, inr_to_usd
                )
                entry["global_advance_sales_from_tomorrow_usd"] += _usd_amount(
                    advance_from_tomorrow, currency, inr_to_usd
                )
                entry["global_advance_sales_from_tomorrow_inr"] += _inr_amount(
                    advance_from_tomorrow, currency, inr_to_usd
                )
            except ValueError:
                LOGGER.exception(
                    "Skipping currency conversion for %s/%s with currency %s.",
                    market_id,
                    global_slug,
                    item.get("currency") or portal.get("currency"),
                )
            entry["global_tickets"] += _integer(current.get("total_tickets"))
            entry["global_advance_tickets"] += _integer(advance.get("advance_tickets"))
            entry["global_tickets_so_far"] += _integer(
                item.get("total_tickets_so_far")
            )
            entry["global_advance_tickets_from_tomorrow"] += _integer(
                item.get("advance_tickets_from_tomorrow")
            )
            entry["global_shows_so_far"] += _integer(item.get("total_shows_so_far"))
            entry["global_advance_shows_from_tomorrow"] += _integer(
                item.get("advance_shows_from_tomorrow")
            )
            entry["last_tracked_date"] = max(
                str(entry["last_tracked_date"]),
                str(item.get("last_tracked_date") or ""),
            )
            if market_id not in entry["markets"]:
                entry["markets"].append(market_id)

    for global_slug, entry in reconciled.items():
        previous = previous_catalog.get(global_slug, {})
        sources = artwork_sources[global_slug]
        primary_source = next(
            (item for market_id, item in sources if market_id == primary_market),
            {},
        )
        for field in ("poster_url", "cover_url", "release_date"):
            current_value = primary_source.get(field)
            previous_value = previous.get(field)
            other_market_value = next(
                (item.get(field) for market_id, item in sources
                 if market_id != primary_market and item.get(field)),
                "",
            )
            entry[field] = current_value or previous_value or other_market_value or ""

    global_catalog = sorted(
        reconciled.values(),
        key=lambda item: (str(item.get("title", "")).lower(), item["global_slug"]),
    )

    def spotlight(gross_key: str, tickets_key: str) -> dict | None:
        eligible = [item for item in global_catalog if _number(item.get(gross_key)) > 0]
        if not eligible:
            return None
        winner = max(
            eligible,
            key=lambda item: (_number(item.get(gross_key)), item["global_slug"]),
        )
        return {
            "global_slug": winner["global_slug"],
            "title": winner["title"],
            "cover_url": winner["cover_url"],
            "poster_url": winner["poster_url"],
            gross_key: winner[gross_key],
            tickets_key: winner[tickets_key],
            "global_gross_usd": winner["global_gross_usd"],
            "global_gross_inr": winner["global_gross_inr"],
            "global_advance_usd": winner["global_advance_usd"],
            "global_advance_inr": winner["global_advance_inr"],
            "market_metrics": winner["market_metrics"],
            "active_markets": winner["markets"],
        }

    top10_playing_now = _ranked_lifecycle(
        global_catalog,
        "playing_now",
        "global_total_gross_so_far_usd",
        "global_tickets_so_far",
        "global_slug",
    )
    top10_coming_soon = _ranked_lifecycle(
        global_catalog,
        "coming_soon",
        "global_advance_sales_from_tomorrow_usd",
        "global_advance_tickets_from_tomorrow",
        "global_slug",
    )

    def lifecycle_spotlight(items: list[dict]) -> dict | None:
        return dict(items[0]) if items else None

    return {
        "schema_version": 2,
        "updated_at": updated_at,
        "meta_exchange_rates": {
            "base_currency": "USD",
            "INR_TO_USD": inr_to_usd,
            "recorded_at": updated_at,
        },
        "top_spotlight_current": spotlight("global_gross_usd", "global_tickets"),
        "top_spotlight_advance": spotlight(
            "global_advance_usd", "global_advance_tickets"
        ),
        "top10_playing_now": top10_playing_now,
        "top10_coming_soon": top10_coming_soon,
        "top_spotlight_playing_now": lifecycle_spotlight(top10_playing_now),
        "top_spotlight_coming_soon": lifecycle_spotlight(top10_coming_soon),
        "global_catalog": global_catalog,
    }


def build_portal_views(
    db: Any,
    market_ids: tuple[str, ...] = DEFAULT_MARKETS,
    as_of_date: str | date | None = None,
    updated_at: str | None = None,
    inr_to_usd: float = DEFAULT_INR_TO_USD,
) -> dict:
    """Build portal documents using only rollups, published pointers, and summaries."""
    if inr_to_usd <= 0:
        raise ValueError("INR_TO_USD must be greater than zero.")
    timestamp = updated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    market_portals = {}

    for market_id in market_ids:
        market_root = f"markets/{market_id}"
        try:
            if isinstance(as_of_date, str):
                effective_date = date.fromisoformat(as_of_date)
            else:
                effective_date = as_of_date or _market_today(market_id)
            portal = _build_market_portal(db, market_id, effective_date, timestamp)
            db.reference(f"{market_root}/portal_view").set(portal)
            market_portals[market_id] = portal
            LOGGER.info(
                "Published %s market portal: %d movies, %d playing now, %d coming soon, %d provisional occupancy values.",
                market_id,
                len(portal["movie_catalog"]),
                len(portal["top10_playing_now"]),
                len(portal["top10_coming_soon"]),
                sum(
                    bool(
                        _as_dict(item.get("current")).get(
                            "avg_occupancy_is_estimate"
                        )
                    )
                    for item in portal["movie_catalog"]
                ),
            )
        except Exception:
            LOGGER.exception("Could not build market portal for %s.", market_id)
            previous_portal = _as_dict(db.reference(f"{market_root}/portal_view").get())
            if previous_portal:
                market_portals[market_id] = previous_portal

    for market_id in DEFAULT_MARKETS:
        if market_id in market_portals:
            continue
        previous_portal = _as_dict(
            db.reference(f"markets/{market_id}/portal_view").get()
        )
        if previous_portal:
            market_portals[market_id] = previous_portal

    if not market_portals:
        raise RuntimeError("No market portal could be built or reused; global view not written.")

    previous_global_portal = _as_dict(db.reference("global/portal_view").get())
    global_portal = _build_global_portal(
        market_portals,
        timestamp,
        inr_to_usd,
        previous_global_portal.get("global_catalog"),
    )
    db.reference("global/portal_view").set(global_portal)
    return {
        "markets": market_portals,
        "global": global_portal,
    }


def run_portal_views(
    market_ids: tuple[str, ...],
    inr_to_usd: float,
    as_of_date: str | date | None = None,
    db_instance: Any | None = None,
) -> dict:
    """Run the portal build directly from a scraper or initialize Firebase."""
    if not market_ids:
        raise ValueError("At least one market ID must be provided.")
    database = db_instance if db_instance is not None else _initialize_firebase()
    return build_portal_views(
        database,
        market_ids=market_ids,
        as_of_date=as_of_date,
        inr_to_usd=inr_to_usd,
    )


def _initialize_firebase():
    try:
        import firebase_admin
        from firebase_admin import credentials, db
    except ImportError as exc:
        raise RuntimeError(
            "firebase-admin is required to run build_portal_views.py."
        ) from exc

    firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
    firebase_creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE")
    database_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not database_url or not (firebase_creds_json or firebase_creds_file):
        raise RuntimeError(
            "Set FIREBASE_DATABASE_URL and either FIREBASE_CREDENTIALS "
            "or FIREBASE_CREDENTIALS_FILE."
        )

    if not firebase_admin._apps:
        if firebase_creds_json:
            credential_data = json.loads(firebase_creds_json)
        else:
            with open(firebase_creds_file, encoding="utf-8") as credentials_file:
                credential_data = json.load(credentials_file)
        firebase_admin.initialize_app(
            credentials.Certificate(credential_data),
            {"databaseURL": database_url},
        )
    return db


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv
    except ImportError:
        LOGGER.warning("python-dotenv is unavailable; using process environment variables.")
    else:
        load_dotenv()

    markets_value = os.environ.get("PORTAL_MARKETS", "")
    if not markets_value.strip():
        raise RuntimeError("Set PORTAL_MARKETS to a comma-separated list of market IDs.")
    markets = tuple(market.strip() for market in markets_value.split(",") if market.strip())
    if not markets:
        raise ValueError("PORTAL_MARKETS must contain at least one market ID.")
    try:
        inr_to_usd = float(os.environ["INR_TO_USD"])
    except KeyError as exc:
        raise RuntimeError("Set INR_TO_USD in the process environment.") from exc
    except ValueError as exc:
        raise ValueError("INR_TO_USD must be a positive number.") from exc
    if inr_to_usd <= 0:
        raise ValueError("INR_TO_USD must be greater than zero.")
    run_portal_views(
        market_ids=markets,
        inr_to_usd=inr_to_usd,
    )


if __name__ == "__main__":
    main()
