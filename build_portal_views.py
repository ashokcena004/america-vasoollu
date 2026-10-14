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
) -> tuple[dict[str, dict], dict[str, dict]]:
    current_summaries = {}
    advance_summaries = {}

    for show_date in sorted(rollup_dates):
        day = _date_value(show_date)
        if day is None:
            LOGGER.warning("Skipping invalid show date %s/%s/%s.", market_id, movie_id, show_date)
            continue

        rollup = _as_dict(rollup_dates[show_date])

        published = _as_dict(
            db.reference(
                f"markets/{market_id}/movies/{movie_id}/dates/{show_date}/published"
            ).get()
        )
        if (
            day <= as_of_date
            and _scope_available(date_index, show_date, "current", day, as_of_date)
            and isinstance(rollup.get("current"), dict)
        ):
            run_id = published.get("active_run_id")
            if run_id:
                summary = _as_dict(
                    _summary_reference(db, market_id, movie_id, show_date, str(run_id)).get()
                )
                if summary:
                    current_summaries[show_date] = summary
                else:
                    LOGGER.warning(
                        "Missing active analytics summary for %s/%s/%s run %s.",
                        market_id,
                        movie_id,
                        show_date,
                        run_id,
                    )
            else:
                LOGGER.warning(
                    "Missing active run pointer for %s/%s/%s.",
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

    return current_summaries, advance_summaries


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
        if scope == "current" and day > as_of_date:
            continue
        if not _scope_available(date_index, show_date, scope, day, as_of_date):
            continue
        contribution = _as_dict(_as_dict(entry).get(scope))
        totals["gross"] += _number(contribution.get("booked_gross"))
        totals["tickets"] += _integer(contribution.get("booked_tickets"))
        totals["shows"] += _integer(contribution.get("show_count"))
    return totals


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
    key = f"has_{scope}"
    if key in entry:
        return bool(entry[key])
    return show_day <= as_of_date if scope == "current" else show_day > as_of_date


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
        contribution = _as_dict(_as_dict(rollup_dates[show_date]).get(scope))
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
    date_index = _as_dict(db.reference(f"{root}/date_index").get())
    cumulative = _as_dict(db.reference(f"{root}/cumulative/v1/bases").get())
    basis, basis_data = _choose_basis(cumulative, market_id, metadata, movie_index)
    months = _as_dict(basis_data.get("months"))
    if not months:
        LOGGER.warning("No cumulative monthly rollups for %s/%s.", market_id, movie_id)

    rollup_dates = {}
    for month in months.values():
        rollup_dates.update(_as_dict(_as_dict(month).get("dates")))

    current_totals = _scope_totals(rollup_dates, date_index, "current", as_of_date)
    advance_totals = _scope_totals(rollup_dates, date_index, "advance", as_of_date)
    current_summaries, advance_summaries = _read_date_summaries(
        db,
        market_id,
        movie_id,
        rollup_dates,
        date_index,
        as_of_date,
    )

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
    tracked_dates = [
        show_date
        for show_date, entry in rollup_dates.items()
        if _as_dict(entry).get("current") or _as_dict(entry).get("advance")
    ]
    meta = {
        "title": title,
        "global_slug": global_slug,
        "poster_url": metadata.get("poster_url") or movie_index.get("poster_url") or "",
        "cover_url": metadata.get("cover_url") or movie_index.get("cover_url") or "",
        "release_date": metadata.get("release_date") or movie_index.get("release_date") or "",
        "last_tracked_date": max(tracked_dates, default=""),
    }
    currency = str(
        metadata.get("currency")
        or basis_data.get("currency")
        or movie_index.get("currency")
        or MARKET_CURRENCY.get(market_id.lower(), "USD")
    )
    latest_current = _latest_metrics(current_summaries)
    latest_advance = _latest_metrics(advance_summaries)
    current = {
        "total_gross": current_totals["gross"],
        "total_tickets": current_totals["tickets"],
        "total_shows": current_totals["shows"],
        "avg_occupancy": _occupancy_rollup(current_summaries),
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
        "avg_occupancy": _occupancy_rollup(advance_summaries),
        "fast_fillings": _integer(latest_advance.get("fast_fillings")),
        "sold_outs": _integer(latest_advance.get("sold_outs")),
        "gross_delta": _delta(latest_advance, "gross_delta", "booked_gross"),
        "tickets_delta": _delta(latest_advance, "tickets_delta", "booked_tickets"),
        "shows_delta": _delta(latest_advance, "shows_delta", "show_count"),
        "venues_delta": _delta(latest_advance, "venues_delta", "theater_count"),
    }

    summary_view = {
        "schema_version": 1,
        "market_id": market_id,
        "movie_id": movie_id,
        "currency": currency,
        "updated_at": updated_at,
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
            "advance_avg_occupancy": _occupancy_rollup(advance_summaries),
        },
        "daily_trajectory_current": _build_trajectory(
            rollup_dates, current_summaries, date_index, "current", as_of_date
        ),
        "daily_trajectory_advance": _build_trajectory(
            rollup_dates, advance_summaries, date_index, "advance", as_of_date
        ),
        "gross_basis": basis,
    }
    catalog_item = {
        "movie_id": movie_id,
        **meta,
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
        "schema_version": 1,
        "market_id": market_id,
        "currency": currency,
        "updated_at": updated_at,
        "movie_catalog": catalog,
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
                    "market_metrics": {},
                    "markets": [],
                }
            entry = reconciled[global_slug]
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
                },
            )
            market_metrics["current"]["gross"] += _number(current.get("total_gross"))
            market_metrics["current"]["tickets"] += _integer(current.get("total_tickets"))
            market_metrics["advance"]["gross"] += _number(advance.get("advance_gross"))
            market_metrics["advance"]["tickets"] += _integer(advance.get("advance_tickets"))
            try:
                current_gross = _number(current.get("total_gross"))
                advance_gross = _number(advance.get("advance_gross"))
                entry["global_gross_usd"] += _usd_amount(current_gross, currency, inr_to_usd)
                entry["global_gross_inr"] += _inr_amount(current_gross, currency, inr_to_usd)
                entry["global_advance_usd"] += _usd_amount(advance_gross, currency, inr_to_usd)
                entry["global_advance_inr"] += _inr_amount(advance_gross, currency, inr_to_usd)
            except ValueError:
                LOGGER.exception(
                    "Skipping currency conversion for %s/%s with currency %s.",
                    market_id,
                    global_slug,
                    item.get("currency") or portal.get("currency"),
                )
            entry["global_tickets"] += _integer(current.get("total_tickets"))
            entry["global_advance_tickets"] += _integer(advance.get("advance_tickets"))
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

    return {
        "schema_version": 1,
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
