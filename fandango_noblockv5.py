import asyncio
from datetime import datetime, timezone, timedelta
import json
import logging
import os
import re
import time
import traceback
from dotenv import load_dotenv
import pytz
from playwright.async_api import async_playwright

load_dotenv()

import firebase_admin
from firebase_admin import credentials, db
from utils.analytics_v2 import (
    build_analytics_payload,
    classify_report_formats,
    combine_report_format_categories,
    publish_analytics_payload,
)

from utils.generateFandangoExcelReport import export_master_excel
from utils.generateFandangoHTMLReport import generate_fandango_html_report
from utils.generateFandangoImageReport import generate_fandango_image_report

# =============================================================================
# ── 1. CONFIGURATION ─────────────────────────────────────────────────────────
# =============================================================================

FANDANGO_QUEUE_NEXT_INDEX = None

# =============================================================================

USE_FIREBASE = True

OVERRIDE_LOCAL_SNAPSHOT = True  # ⚠️ Set to True to update the baseline after this run

OVERRIDE_FIREBASE_DAILY_SNAPSHOT = False  # ⚠️ Set to True to update the Firebase snapshot after this run

# 🚫 IGNORE SOLD OUT SHOWS FOR SPECIFIC CHAINS
# If a show at these chains is "Sold Out", its tickets and gross will be zeroed out.
IGNORE_SOLDOUT_CHAINS = ["AMC"]

# 🚀 HIGH-SPEED ENGINE TUNING PARAMETERS
MAX_WORKERS = 8                     # Independent Chromium processes (separate TLS/HTTP2 sockets)
THEATER_BATCH_SIZE = 3              # Theaters checked per JS batch call
SEATMAP_CONCURRENCY_PER_WORKER = 2  # Parallel seatMap fetches inside each worker's browser
PROACTIVE_ROTATE_LIMIT = 320        # Refresh Akamai cookie before the ~440 request cliff
MAX_API_RETRIES = 3                 # Automatic retries on 403/429/timeout per request

# =============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MOVIE_CONFIG_PATH = os.path.join(SCRIPT_DIR, "data", "movie_config.json")
FANDANGO_QUEUE_PATH = os.path.join(SCRIPT_DIR, "data", "fandango_scrape_queue.json")


def load_fandango_scrape_queue(queue_path):
    try:
        with open(queue_path, "r", encoding="utf-8") as queue_file:
            queue_config = json.load(queue_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load Fandango scrape queue at {queue_path}.") from exc

    if not isinstance(queue_config, dict) or not isinstance(queue_config.get("queue"), list):
        raise ValueError(
            f"Fandango scrape queue must be an object containing a 'queue' list: {queue_path}"
        )

    scrape_queue = []
    for index, item in enumerate(queue_config["queue"]):
        if not isinstance(item, dict):
            raise ValueError(f"Queue item {index + 1} must be an object.")
        global_slug = item.get("global_slug")
        show_date = item.get("show_date")
        force_update_advance = item.get("force_update_advance", False)
        if not isinstance(global_slug, str) or not global_slug.strip():
            raise ValueError(f"Queue item {index + 1} must provide a non-empty 'global_slug'.")
        if not isinstance(force_update_advance, bool):
            raise ValueError(
                f"Queue item {index + 1} field 'force_update_advance' must be a boolean."
            )
        if not isinstance(show_date, str):
            raise ValueError(f"Queue item {index + 1} must provide 'show_date' in YYYY-MM-DD format.")
        try:
            parsed_show_date = datetime.strptime(show_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"Queue item {index + 1} has an invalid 'show_date': {show_date!r}."
            ) from exc
        if parsed_show_date.strftime("%Y-%m-%d") != show_date:
            raise ValueError(
                f"Queue item {index + 1} has an invalid 'show_date': {show_date!r}."
            )
        scrape_queue.append((global_slug.strip(), show_date, force_update_advance))
    return scrape_queue


FANDANGO_SCRAPE_QUEUE = load_fandango_scrape_queue(FANDANGO_QUEUE_PATH)
if not FANDANGO_SCRAPE_QUEUE:
    print("ℹ️ FANDANGO_SCRAPE_QUEUE is empty; skipping scraper.")
    raise SystemExit(0)

firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
firebase_creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE")
firebase_db_url = os.environ.get("FIREBASE_DATABASE_URL")
if (not firebase_creds_json and not firebase_creds_file) or not firebase_db_url:
    raise RuntimeError("Firebase credentials and database URL are required for queue mode.")
if not firebase_admin._apps:
    if firebase_creds_json:
        queue_creds_dict = json.loads(firebase_creds_json)
    else:
        with open(firebase_creds_file, "r", encoding="utf-8") as credentials_file:
            queue_creds_dict = json.load(credentials_file)
    firebase_admin.initialize_app(
        credentials.Certificate(queue_creds_dict),
        {"databaseURL": firebase_db_url},
    )

queue_index = db.reference("fandango_cron_currentindex").get()
if queue_index is None:
    queue_index = 0
elif type(queue_index) is not int:
    raise ValueError("Firebase fandango_cron_currentindex must be an integer or null.")
if queue_index < 0 or queue_index >= len(FANDANGO_SCRAPE_QUEUE):
    queue_index = 0

GLOBAL_MOVIE_SLUG, SHOW_DATE, FORCE_UPDATE_ADVANCE = FANDANGO_SCRAPE_QUEUE[queue_index]
FANDANGO_QUEUE_NEXT_INDEX = queue_index + 1
print(
    f"📋 Queue item {queue_index + 1}/{len(FANDANGO_SCRAPE_QUEUE)}: "
    f"{GLOBAL_MOVIE_SLUG} on {SHOW_DATE}"
)


def parse_movie_targets(raw_movie_url_field, extra_ids_field=None):
    """
    Accepts a single string, comma-separated string, or a list of strings/ints from
    movie_config.json ('movie_url', 'movie_urls', 'movie_ids', 'additional_movie_ids').
    Each item can be:
      - Full URL: "https://www.fandango.com/scene-2026-248114/movie-overview"
      - Slug + ID: "scene-2026-248114"
      - Numeric ID: "248114" or 248114
    Returns: (primary_url, primary_slug, primary_id, ordered_unique_ids_list)
    """
    combined_inputs = []
    for source in (raw_movie_url_field, extra_ids_field):
        if source is None:
            continue
        if isinstance(source, list):
            combined_inputs.extend(source)
        else:
            combined_inputs.append(source)

    raw_items = []
    for entry in combined_inputs:
        for part in str(entry or "").split(","):
            if part.strip():
                raw_items.append(part.strip())

    parsed_entries = []
    for s in raw_items:
        # 1. Full URL match
        url_m = re.search(
            r"fandango\.com/([a-zA-Z0-9-]+?)-(\d+)(?:/|$|\?)", s, re.IGNORECASE
        )
        if url_m:
            slug_part, id_part = url_m.group(1), url_m.group(2)
            full_u = (
                s
                if s.startswith("http")
                else f"https://www.fandango.com/{slug_part}-{id_part}/movie-overview"
            )
            parsed_entries.append((full_u, slug_part, id_part))
            continue

        # 2. Slug + ID match (e.g. "scene-2026-248114")
        slug_m = re.search(r"^([a-zA-Z0-9-]+?)-(\d+)$", s)
        if slug_m:
            slug_part, id_part = slug_m.group(1), slug_m.group(2)
            full_u = f"https://www.fandango.com/{slug_part}-{id_part}/movie-overview"
            parsed_entries.append((full_u, slug_part, id_part))
            continue

        # 3. Raw Numeric ID match (e.g. "248114")
        if s.isdigit():
            parsed_entries.append(("", "", s))
            continue

    if not parsed_entries:
        raise ValueError(
            f"Could not extract any valid Fandango Movie ID from movie_url: {raw_movie_url_field!r}"
        )

    primary_url, primary_slug, primary_id = parsed_entries[0]
    if not primary_slug:
        for u, sl, _ in parsed_entries:
            if sl:
                primary_slug = sl
                if not primary_url:
                    primary_url = u
                break
    if not primary_slug:
        primary_slug = f"movie-{primary_id}"
    if not primary_url:
        primary_url = (
            f"https://www.fandango.com/{primary_slug}-{primary_id}/movie-overview"
        )

    all_ids = []
    for _, _, mid in parsed_entries:
        if mid and mid not in all_ids:
            all_ids.append(mid)

    return primary_url, primary_slug, primary_id, all_ids


def load_movie_config(config_path, global_slug):
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            movie_configs = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not load movie configuration at {config_path}."
        ) from exc

    if not isinstance(movie_configs, dict):
        raise ValueError(f"Movie configuration must be a JSON object: {config_path}")
    movie_config = movie_configs.get(global_slug)
    if not isinstance(movie_config, dict):
        raise KeyError(
            f"No movie configuration found for global slug {global_slug!r} "
            f"in {config_path}."
        )

    raw_url_field = (
        movie_config.get("movie_url")
        or movie_config.get("movie_urls")
        or movie_config.get("movie_ids")
        or movie_config.get("additional_movie_ids")
    )
    if not raw_url_field or (
        isinstance(raw_url_field, str) and not raw_url_field.strip()
    ):
        raise ValueError(
            f"Movie configuration for {global_slug!r} must provide "
            "a non-empty 'movie_url' (string or list)."
        )
    if (
        not isinstance(movie_config.get("default_language"), str)
        or not movie_config["default_language"].strip()
    ):
        raise ValueError(
            f"Movie configuration for {global_slug!r} must provide "
            "a non-empty 'default_language'."
        )
    for field in ("average_price", "max_price"):
        value = movie_config.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(
                f"Movie configuration field {field!r} for {global_slug!r} "
                "must be a non-negative number."
            )
    for field in ("poster_url", "cover_url", "release_date"):
        if field in movie_config and not isinstance(movie_config[field], str):
            raise ValueError(
                f"Movie configuration field {field!r} for {global_slug!r} "
                "must be a string."
            )
    extra = movie_config.get("extra", [])
    if not isinstance(extra, list):
        raise ValueError(
            f"Movie configuration field 'extra' for {global_slug!r} must be a list."
        )
    if extra and (
        len(extra) != 6
        or not isinstance(extra[0], str)
        or extra[0].upper() != "EXTRA"
    ):
        raise ValueError(
            f"Movie configuration field 'extra' for {global_slug!r} must be "
            'empty or shaped like ["EXTRA", "", "", gross, 0, tickets].'
        )

    return movie_config


FALLBACK_SEATS = 100
PRICE_TAX_CUT = False  # If True, rounds ticket prices down to nearest multiple of 5 (e.g., $29 -> $25)
OCC_THRESHOLD_FRONTROW = 0.50  # If overall occupancy is below this, treat front row 'R' seats as blocks

MOVIE_CONFIG = load_movie_config(MOVIE_CONFIG_PATH, GLOBAL_MOVIE_SLUG)
RAW_MOVIE_URL_CONFIG = [
    entry
    for field in ("movie_url", "movie_urls", "movie_ids", "additional_movie_ids")
    for entry in (
        MOVIE_CONFIG[field]
        if isinstance(MOVIE_CONFIG.get(field), list)
        else [MOVIE_CONFIG.get(field)]
    )
    if MOVIE_CONFIG.get(field) not in (None, "")
]

URL, MOVIE_SLUG, MOVIE_ID, TARGET_MOVIE_IDS = parse_movie_targets(
    RAW_MOVIE_URL_CONFIG
)
TARGET_MOVIE_IDS_SET = set(TARGET_MOVIE_IDS)

AVG_PRICE = float(MOVIE_CONFIG["average_price"])
MAX_TIER_PRICE = float(MOVIE_CONFIG["max_price"])
DEFAULT_LANGUAGE = MOVIE_CONFIG["default_language"]
SUPPORTED_LANGUAGES = ("TELUGU", "HINDI", "TAMIL", "MALAYALAM", "KANNADA", "ENGLISH")
MOVIE_METADATA = {
    field: MOVIE_CONFIG.get(field, "").strip()
    for field in ("poster_url", "cover_url", "release_date")
}

if "ignore_soldout_chains" in MOVIE_CONFIG and isinstance(
    MOVIE_CONFIG["ignore_soldout_chains"], list
):
    IGNORE_SOLDOUT_CHAINS = MOVIE_CONFIG["ignore_soldout_chains"]


def detect_show_language(amenities, default_language, movie_title=""):
    language_pattern = re.compile(
        rf"^\s*({'|'.join(SUPPORTED_LANGUAGES)})\s+(?:WITH\b|LANGUAGE\b)",
        re.IGNORECASE,
    )
    for amenity in amenities:
        match = language_pattern.match(str(amenity))
        if match:
            return match.group(1).title()
    if movie_title:
        title_up = str(movie_title).upper()
        for lang in SUPPORTED_LANGUAGES:
            if re.search(rf"\b{lang}\b", title_up):
                return lang.title()
    return default_language


STATE_TIMEZONES = {
    "Alaska": "America/Anchorage",
    "Alabama": "America/Chicago",
    "Arkansas": "America/Chicago",
    "Arizona": "America/Phoenix",
    "California": "America/Los_Angeles",
    "Colorado": "America/Denver",
    "Connecticut": "America/New_York",
    "District Of Columbia": "America/New_York",
    "Delaware": "America/New_York",
    "Florida": "America/New_York",
    "Georgia": "America/New_York",
    "Hawaii": "Pacific/Honolulu",
    "Iowa": "America/Chicago",
    "Idaho": "America/Denver",
    "Illinois": "America/Chicago",
    "Indiana": "America/Indiana/Indianapolis",
    "Kansas": "America/Chicago",
    "Kentucky": "America/New_York",
    "Louisiana": "America/Chicago",
    "Massachusetts": "America/New_York",
    "Maryland": "America/New_York",
    "Maine": "America/New_York",
    "Michigan": "America/Detroit",
    "Minnesota": "America/Chicago",
    "Missouri": "America/Chicago",
    "Mississippi": "America/Chicago",
    "Montana": "America/Denver",
    "North Carolina": "America/New_York",
    "North Dakota": "America/Chicago",
    "Nebraska": "America/Chicago",
    "New Hampshire": "America/New_York",
    "New Jersey": "America/New_York",
    "New Mexico": "America/Denver",
    "Nevada": "America/Los_Angeles",
    "New York": "America/New_York",
    "Ohio": "America/New_York",
    "Oklahoma": "America/Chicago",
    "Oregon": "America/Los_Angeles",
    "Pennsylvania": "America/New_York",
    "RhodeIsland": "America/New_York",
    "Rhode Island": "America/New_York",
    "South Carolina": "America/New_York",
    "South Dakota": "America/Chicago",
    "Tennessee": "America/Chicago",
    "Texas": "America/Chicago",
    "Utah": "America/Denver",
    "Virginia": "America/New_York",
    "Vermont": "America/New_York",
    "Washington": "America/Los_Angeles",
    "Wisconsin": "America/Chicago",
    "West Virginia": "America/New_York",
    "Wyoming": "America/Denver",
    "American Samoa": "Pacific/Pago_Pago",
    "Guam": "Pacific/Guam",
}


def _clean_cumulative_value(value):
    return str(value).replace(" ", "").replace("\u202f", "").lower()


def _parse_fandango_time(value):
    if not value:
        return None
    normalized = str(value).strip().lower().replace("o'clock", ":00").replace(" ", "")
    try:
        return datetime.strptime(normalized, "%I:%M%p").time()
    except ValueError:
        return None


def _get_show_status(booked, total, has_available_failed_tiers):
    if has_available_failed_tiers:
        return "Available (Map Error)"
    return "Sold Out" if booked >= total and total > 0 else "Available"


def reconcile_cumulative_shows(
    current_shows, summary_data, previous_shows, show_date, now=None
):
    current_by_key = {}
    for index, row in enumerate(current_shows):
        key = (
            f"{row.get('t_id')}_{_clean_cumulative_value(row.get('time'))}_"
            f"{_clean_cumulative_value(row.get('format'))}"
        )
        current_by_key.setdefault(key, []).append({"index": index, "data": row})

    show_date_obj = datetime.strptime(show_date, "%Y-%m-%d").date()
    rescued = 0
    protected = 0
    dropped = 0

    for previous in previous_shows:
        if previous.get("is_extra") or previous.get("format") == "Fan Event / Manual":
            continue

        theater_id = previous.get("t_id")
        key = (
            f"{theater_id}_{_clean_cumulative_value(previous.get('time'))}_"
            f"{_clean_cumulative_value(previous.get('format'))}"
        )
        matches = current_by_key.get(key, [])

        if not matches:
            state = previous.get("state", "Illinois")
            timezone_name = STATE_TIMEZONES.get(state, "America/Chicago")
            is_future_show = False
            try:
                local_timezone = pytz.timezone(timezone_name)
                if now is None:
                    current_local_time = datetime.now(local_timezone)
                elif now.tzinfo is None:
                    current_local_time = local_timezone.localize(now)
                else:
                    current_local_time = now.astimezone(local_timezone)
                parsed_time = _parse_fandango_time(previous.get("time"))
                if parsed_time:
                    show_datetime = local_timezone.localize(
                        datetime.combine(show_date_obj, parsed_time)
                    )
                    is_future_show = (
                        show_datetime > current_local_time + timedelta(hours=1)
                    )
            except (
                pytz.UnknownTimeZoneError,
                pytz.InvalidTimeError,
                ValueError,
                TypeError,
                OverflowError,
            ):
                is_future_show = False

            if is_future_show:
                dropped += 1
                continue

            current_shows.append(previous)
            if theater_id not in summary_data:
                summary_data[theater_id] = {
                    "t_id": theater_id,
                    "state": previous.get("state", "Unknown"),
                    "name": previous.get("theater", "Unknown"),
                    "shows": 0,
                    "total": 0,
                    "booked": 0,
                    "gross": 0.0,
                }
            summary = summary_data[theater_id]
            summary["shows"] += 1
            summary["total"] += previous.get("total", 0)
            summary["booked"] += previous.get("booked", 0)
            summary["gross"] += previous.get("gross", 0.0)
            rescued += 1
            print(
                f"🛡️ RESCUED: {previous.get('theater')} at {previous.get('time')} "
                f"- Maintained ${previous.get('gross', 0):.2f}"
            )
            continue

        current_entry = matches.pop(0)
        current = current_entry["data"]
        if current.get("status") != "Available (Map Error)":
            continue

        summary = summary_data[theater_id]
        summary["total"] -= current.get("total", 0)
        summary["booked"] -= current.get("booked", 0)
        summary["gross"] -= current.get("gross", 0.0)
        current_shows[current_entry["index"]] = previous
        summary["total"] += previous.get("total", 0)
        summary["booked"] += previous.get("booked", 0)
        summary["gross"] += previous.get("gross", 0.0)
        protected += 1

    return {
        "shows": current_shows,
        "rescued": rescued,
        "protected": protected,
        "dropped": dropped,
    }


# 📝 MANUAL SHOWS OVERRIDE (Fan Shows / Missing API Data)
MANUAL_SHOWS = [
    # ["EXTRA", "", "", 3300.0, 0, 115],
    # ["Illinois", "AABCD", "7:00 PM", 1500.0, 3000.0, 75, 150, 0.50, "IMAX", "Telugu"],
]

EXTRA_GROSS_NOTE = "Added extra gross for offline fans shows."
if MOVIE_CONFIG.get("extra"):
    MANUAL_SHOWS.append(MOVIE_CONFIG["extra"])


# =============================================================================
# ── 1.1 MAPPING, HASHING AND SNAPSHOT CONFIGURATION ──────────────────────────
# =============================================================================

MAPPING_FILE = "state_theatre_mapping.json"

SNAPSHOT_DIR = "snapshots"
HASH_CACHE_DIR = "cached_capacities"


def format_movie_title(movie_slug):
    title_words = movie_slug.replace("-", " ").title().split()
    if len(title_words) > 1 and re.fullmatch(r"(?:19|20)\d{2}", title_words[-1]):
        title_words.pop()
    return " ".join(title_words)


MOVIE_TITLE = format_movie_title(MOVIE_SLUG)

os.makedirs(SNAPSHOT_DIR, exist_ok=True)
os.makedirs(HASH_CACHE_DIR, exist_ok=True)

LAST_SNAPSHOT_FILE = os.path.join(
    SNAPSHOT_DIR,
    f"{MOVIE_SLUG}_{SHOW_DATE}_snapshot.json",
)

HASH_CACHE_FILE = os.path.join(
    HASH_CACHE_DIR,
    f"{MOVIE_SLUG}_{SHOW_DATE}_hash_cache.json",
)


# =============================================================================
# ── 1.2 PROCESSING CONFIGURATION ─────────────────────────────────────────────
# =============================================================================

SEAT_COUNT_MODE = 1
# 1 = Use physical seat map count when available (more accurate)
# 2 = Use cached summary data from API

# 🎯 SCREEN EXCLUSION CONFIGURATION
# Format: "TheaterID_AuditoriumID"
PREFER_CACHED_SCREENS = [
    "9E1KKd3a_10",  # Cinemark Merriam and XD - Aud 10
    "9E1KKd3a_25",  # Cinemark Merriam and XD - Aud 25
]
PREFER_PHYSICAL_SCREENS = []

# 🔧 AUTOMATIC MULTI-TIER THEATER MERGE LIST
MANUAL_MERGE_SHOWS_THEATRES = ["AAILI"]

# =============================================================================
# FIREBASE CONFIGURATION
# =============================================================================
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS", "")
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "")
FIREBASE_ROOT_PATH = os.getenv("FIREBASE_ROOT_PATH", "markets/usa/movies")
FIREBASE_LOCAL_HASH_WINS = True


def initialize_firebase():
    """Initialize Firebase using service-account credentials from the environment."""
    if not USE_FIREBASE:
        print("🔥 Firebase disabled (USE_FIREBASE=False).")
        return False
    if firebase_admin is None:
        print("⚠️ firebase-admin is not installed.")
        return False
    try:
        firebase_creds_json = os.getenv("FIREBASE_CREDENTIALS")
        firebase_creds_file = os.getenv("FIREBASE_CREDENTIALS_FILE")
        firebase_db_url = os.getenv("FIREBASE_DATABASE_URL")
        if not firebase_creds_json and not firebase_creds_file:
            print("⚠️ FIREBASE_CREDENTIALS or FIREBASE_CREDENTIALS_FILE is missing.")
            return False
        if not firebase_db_url:
            print("⚠️ FIREBASE_DATABASE_URL is missing.")
            return False

        if firebase_creds_json:
            creds_dict = json.loads(firebase_creds_json)
        else:
            with open(firebase_creds_file, "r", encoding="utf-8") as credentials_file:
                creds_dict = json.load(credentials_file)
        cred = credentials.Certificate(creds_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred, {"databaseURL": firebase_db_url})
        print("🔥 Firebase initialized successfully.")
        return True
    except Exception as e:
        print(f"⚠️ Firebase initialization failed: {e}")
        traceback.print_exc()
        return False


def firebase_movie_root():
    """Return the V2 Firebase date node for this movie."""
    return f"{FIREBASE_ROOT_PATH}/{MOVIE_SLUG}/dates/{SHOW_DATE}"


def load_firebase_hash_cache():
    """Read the Firebase hash cache. Returns {} on any failure."""
    if not USE_FIREBASE:
        return {}
    try:
        ref = db.reference(f"{firebase_movie_root()}/scraper_state/hash_capacity_cache")
        data = ref.get()
        if not data:
            print("🔥 No hash cache found in Firebase.")
            return {}
        if not isinstance(data, dict):
            print("⚠️ Firebase hash cache is not a JSON object. Ignoring it.")
            return {}
        print(f"🔥 Loaded {len(data)} hash entries from Firebase.")
        return data
    except Exception as e:
        print(f"⚠️ Failed to load Firebase hash cache: {e}")
        return {}


def merge_hash_caches(local_cache, firebase_cache):
    """Merge local + Firebase hash caches."""
    merged = {}
    conflicts = 0

    if FIREBASE_LOCAL_HASH_WINS:
        merged.update(firebase_cache or {})
        for h_key, h_data in (local_cache or {}).items():
            if h_key in merged and merged[h_key] != h_data:
                conflicts += 1
            merged[h_key] = h_data
    else:
        merged.update(local_cache or {})
        for h_key, h_data in (firebase_cache or {}).items():
            if h_key in merged and merged[h_key] != h_data:
                conflicts += 1
            merged[h_key] = h_data

    if conflicts:
        winner = "local" if FIREBASE_LOCAL_HASH_WINS else "Firebase"
        print(f"⚠️ Hash-cache conflicts found: {conflicts}. {winner} value(s) kept.")

    print(
        f"🧩 Hash cache merged: local={len(local_cache or {})}, "
        f"firebase={len(firebase_cache or {})}, merged={len(merged)}"
    )
    return merged


def load_v2_show_pages(date_root, run_id, page_count):
    """Read the published detail run used for reconciliation and cumulative mode."""
    rows = []
    for page_number in range(1, page_count + 1):
        page = db.reference(
            f"{date_root}/movie_details/runs/{run_id}/pages/{page_number:04d}"
        ).get()
        if not isinstance(page, dict):
            raise RuntimeError(
                f"Published detail page {page_number:04d} is missing for run {run_id}."
            )
        rows.extend(
            {**row, "_analytics_show_id": show_id}
            for show_id, row in page.items()
            if isinstance(row, dict)
        )
    return rows


def append_unpublished_extra_metrics(rows, metrics):
    """Restore manual EXTRA adjustments kept only in the published summary."""
    if not isinstance(metrics, dict):
        return

    totals = {
        "gross": sum(float(row.get("gross") or 0) for row in rows),
        "booked": sum(float(row.get("booked") or 0) for row in rows),
        "total": sum(float(row.get("total") or 0) for row in rows),
    }
    summary_values = {
        "gross": float(metrics.get("booked_gross") or 0),
        "booked": float(metrics.get("booked_tickets") or 0),
        "total": float(metrics.get("capacity_seats") or 0),
    }
    remainder = {key: summary_values[key] - totals[key] for key in totals}
    if any(value < -0.01 for value in remainder.values()):
        print(f"⚠️ Cannot safely reconstruct hidden EXTRA rows; negative remainder: {remainder}")
        return

    extra = {key: max(0, value) for key, value in remainder.items()}
    if any(value > 0.01 for value in extra.values()):
        rows.append({
            "state": "EXTRA",
            "t_id": "EXTRA",
            "theater": "EXTRA",
            "format": "EXTRA",
            "language": "EXTRA",
            "time": "",
            "total": extra["total"],
            "booked": extra["booked"],
            "gross": extra["gross"],
            "is_extra": True,
        })


def save_local_hash_cache(master_hash_cache):
    """Always save the complete merged cache locally."""
    try:
        with open(HASH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(master_hash_cache, f, indent=4)
        print(f"💾 Saved complete hash cache locally: {HASH_CACHE_FILE} ({len(master_hash_cache)} hashes).")
        return True
    except Exception as e:
        print(f"⚠️ Failed to save Hash Cache locally: {e}")
        return False


FIREBASE_READY = initialize_firebase()


# =============================================================================
# ── 1.3 TARGET STATES CONFIGURATION ─────────────────────────────────────────
# =============================================================================

TARGET_STATES = [
    "Alaska", "Alabama", "Arkansas", "Arizona", "California", "Colorado", "Connecticut",
    "District Of Columbia", "Delaware", "Florida", "Georgia", "Hawaii", "Iowa", "Idaho",
    "Illinois", "Indiana", "Kansas", "Kentucky", "Louisiana", "Massachusetts", "Maryland",
    "Maine", "Michigan", "Minnesota", "Missouri", "Mississippi", "Montana", "North Carolina",
    "North Dakota", "Nebraska", "New Hampshire", "New Jersey", "New Mexico", "Nevada",
    "New York", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Virginia", "Vermont", "Washington",
    "Wisconsin", "West Virginia", "Wyoming", "American Samoa", "Guam",
]


# =============================================================================
# ── 2. HIGH-SPEED ASYNC MULTI-BROWSER WORKER ENGINE ─────────────────────────
# =============================================================================

def record_rate_limit(detailed_logger, rate_limit_events, status, endpoint, worker_id):
    event = (
        f"[Worker-{worker_id}] Fandango API returned HTTP {status} "
        f"for {endpoint}"
    )
    rate_limit_events.append(event)
    detailed_logger.warning(event)
    for handler in detailed_logger.handlers:
        handler.flush()
    print(f"RATE LIMIT: {event}", flush=True)


def log_rate_limit_summary(detailed_logger, rate_limit_events):
    summary = (
        f"RATE LIMIT SUMMARY: observed {len(rate_limit_events)} HTTP 403/429 "
        "responses during this run."
    )
    print(summary, flush=True)
    detailed_logger.warning(summary)
    for event in rate_limit_events:
        detailed_logger.warning(event)
    for handler in detailed_logger.handlers:
        handler.flush()


async def refresh_akamai_trust(page, worker_id, detailed_logger):
    """Establishes or refreshes Akamai _abck sensor cookie on the worker's independent browser."""
    overview_url = f"https://www.fandango.com/{MOVIE_SLUG}-{MOVIE_ID}/movie-overview?date={SHOW_DATE}"
    try:
        await page.goto(overview_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2600)
        # Brief synthetic mouse & scroll telemetry so Akamai sensor validates the session
        await page.mouse.move(220 + (worker_id * 15), 260 + (worker_id * 10), steps=4)
        await page.mouse.wheel(0, 200)
        await page.wait_for_timeout(400)
    except Exception as exc:
        detailed_logger.info(f"[Worker-{worker_id}] Trust warmup note: {exc}")


async def fetch_theaters_batch_js(
    page, batch_items, worker_id, detailed_logger, rate_limit_events
):
    """
    Fetches up to THEATER_BATCH_SIZE theater master lists in a single IPC call
    with a 60ms stagger inside JS. Automatically retries any 403/429 after refreshing trust.
    """
    req_payload = [
        {
            "idx": idx,
            "t_id": item[2].get("theaterId"),
            "url": f"https://www.fandango.com/napi/theaterMovieShowtimes/{item[2].get('theaterId')}?chainCode=&startDate={SHOW_DATE}&isdesktop=false",
        }
        for idx, item in enumerate(batch_items)
    ]

    for attempt in range(MAX_API_RETRIES):
        try:
            results = await asyncio.wait_for(
                page.evaluate(
                    """async (items) => {
                        const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                        return await Promise.all(items.map(async (item, i) => {
                            if (i > 0) await sleep(i * 60);
                            const controller = new AbortController();
                            const timer = setTimeout(() => controller.abort(), 9000);
                            try {
                                const response = await fetch(item.url, {
                                    signal: controller.signal,
                                    credentials: "same-origin"
                                });
                                clearTimeout(timer);
                                if (!response.ok) return { idx: item.idx, error: response.status };
                                const data = await response.json().catch(() => null);
                                return { idx: item.idx, data: data };
                            } catch (e) {
                                clearTimeout(timer);
                                return { idx: item.idx, error: "timeout" };
                            }
                        }));
                    }""",
                    req_payload,
                ),
                timeout=14.0,
            )
        except Exception:
            results = [{"idx": i, "error": "eval_timeout"} for i in range(len(batch_items))]

        has_rate_limit = any(
            isinstance(r, dict) and r.get("error") in (403, 429, "timeout", "eval_timeout")
            for r in (results or [])
        )
        for result in results or []:
            if not isinstance(result, dict) or result.get("error") not in (403, 429):
                continue
            result_idx = result.get("idx")
            request = next(
                (item for item in req_payload if item["idx"] == result_idx),
                None,
            )
            endpoint = (
                request["url"]
                if request
                else f"theater batch item {result_idx}"
            )
            record_rate_limit(
                detailed_logger,
                rate_limit_events,
                result["error"],
                endpoint,
                worker_id,
            )
        if not has_rate_limit:
            return results

        if attempt < MAX_API_RETRIES - 1:
            detailed_logger.info(
                f"[Worker-{worker_id}] Theater batch hit 403/429/timeout (Attempt {attempt + 1}). Refreshing Akamai trust..."
            )
            await asyncio.sleep(1.5 + attempt)
            await refresh_akamai_trust(page, worker_id, detailed_logger)

    return results


async def fetch_seatmaps_for_theater_js(
    page, hashes_to_fetch, worker_id, detailed_logger, rate_limit_events
):
    """
    Fetches all seat maps for a single active theater inside one JS evaluation using
    controlled concurrency (SEATMAP_CONCURRENCY_PER_WORKER = 2) and automatic retry on 403/429.
    Returns dict: { tier_hash: response_data_or_error_dict }
    """
    if not hashes_to_fetch:
        return {}

    collected = {}
    pending = list(dict.fromkeys(hashes_to_fetch))

    for attempt in range(MAX_API_RETRIES):
        if not pending:
            break

        req_items = [
            {"hash": h, "url": f"https://www.fandango.com/napi/seatMap/{h}"}
            for h in pending
        ]

        try:
            batch_res = await asyncio.wait_for(
                page.evaluate(
                    """async ({ items, concurrency }) => {
                        const results = {};
                        let cursor = 0;
                        const worker = async () => {
                            while (cursor < items.length) {
                                const index = cursor++;
                                const item = items[index];
                                const controller = new AbortController();
                                const timer = setTimeout(() => controller.abort(), 9000);
                                try {
                                    const response = await fetch(item.url, {
                                        signal: controller.signal,
                                        credentials: "same-origin"
                                    });
                                    clearTimeout(timer);
                                    const data = await response.json().catch(() => null);
                                    if (!response.ok) {
                                        if (Array.isArray(data) && data.length > 0 && data[0].id === 'PerformanceSoldOut') {
                                            results[item.hash] = data;
                                        } else {
                                            results[item.hash] = { error: response.status };
                                        }
                                    } else if (data) {
                                        results[item.hash] = data;
                                    } else {
                                        results[item.hash] = { error: "empty_json" };
                                    }
                                } catch (e) {
                                    clearTimeout(timer);
                                    results[item.hash] = { error: "Fetch Exception: " + e.message };
                                }
                            }
                        };
                        const workers = [];
                        for (let w = 0; w < Math.min(concurrency, items.length); w++) {
                            workers.push(worker());
                        }
                        await Promise.all(workers);
                        return results;
                    }""",
                    {"items": req_items, "concurrency": SEATMAP_CONCURRENCY_PER_WORKER},
                ),
                timeout=max(16.0, len(pending) * 4.5),
            )
        except Exception as exc:
            batch_res = {h: {"error": f"Eval Timeout: {exc}"} for h in pending}

        retry_next = []
        for h in pending:
            res = (batch_res or {}).get(h, {"error": "missing"})
            if isinstance(res, dict) and res.get("error") in (403, 429):
                record_rate_limit(
                    detailed_logger,
                    rate_limit_events,
                    res["error"],
                    f"https://www.fandango.com/napi/seatMap/{h}",
                    worker_id,
                )
            if isinstance(res, dict) and res.get("error") in (403, 429) or (
                isinstance(res, dict)
                and isinstance(res.get("error"), str)
                and ("Exception" in res["error"] or "Timeout" in res["error"])
            ):
                retry_next.append(h)
            else:
                collected[h] = res

        if not retry_next:
            break

        pending = retry_next
        if attempt < MAX_API_RETRIES - 1:
            detailed_logger.info(
                f"[Worker-{worker_id}] Retrying {len(pending)} rate-limited seatMap(s) after Akamai refresh..."
            )
            await asyncio.sleep(1.8 + attempt)
            await refresh_akamai_trust(page, worker_id, detailed_logger)
        else:
            for h in pending:
                collected[h] = (batch_res or {}).get(h, {"error": 403})

    return collected


async def process_theaters_worker_async(
    async_queue,
    worker_id,
    total_tasks,
    detailed_logger,
    master_hash_cache,
    rate_limit_events,
):
    if master_hash_cache is None:
        master_hash_cache = {}

    local_shows_data = []
    local_summary_data = {}
    local_knowledge_base = {}
    local_sold_out_queue = []
    local_ignored_shows_log = []
    local_hash_cache = {}
    local_hash_healed_log = []

    multi_id_mode = len(TARGET_MOVIE_IDS_SET) > 1

    async with async_playwright() as p:
        # Separate Chromium OS process per worker -> Separate TLS/HTTP2 socket to Akamai!
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        print(f"[Thread-{worker_id}] ⏳ Establishing Akamai Trust...")
        detailed_logger.info(f"[Thread-{worker_id}] Establishing Akamai Trust...")
        await refresh_akamai_trust(page, worker_id, detailed_logger)

        reqs_since_refresh = 0
        worker_rotate_limit = PROACTIVE_ROTATE_LIMIT + (worker_id * 20)

        while not async_queue.empty():
            # Proactively refresh Akamai sensor cookie before hitting ~440 request limit
            if reqs_since_refresh >= worker_rotate_limit:
                await refresh_akamai_trust(page, worker_id, detailed_logger)
                reqs_since_refresh = 0

            batch_items = []
            for _ in range(THEATER_BATCH_SIZE):
                try:
                    item = async_queue.get_nowait()
                    if item[2].get("theaterId"):
                        batch_items.append(item)
                except asyncio.QueueEmpty:
                    break

            if not batch_items:
                break

            first_idx, first_state, first_t = batch_items[0]
            print(
                f"[Thread-{worker_id}] 🔍 [{first_idx}/{total_tasks}] "
                f"{first_state[:4].upper()} | {first_t.get('theaterName', 'Unknown')[:35]} "
                f"(+{len(batch_items) - 1} more)"
                if len(batch_items) > 1
                else f"[Thread-{worker_id}] 🔍 [{first_idx}/{total_tasks}] "
                f"{first_state[:4].upper()} | {first_t.get('theaterName', 'Unknown')[:40]}"
            )

            batch_results = await fetch_theaters_batch_js(
                page,
                batch_items,
                worker_id,
                detailed_logger,
                rate_limit_events,
            )
            reqs_since_refresh += len(batch_items)

            for res_obj in batch_results or []:
                b_idx = res_obj.get("idx", 0)
                task_idx, target_state, t = batch_items[b_idx]
                t_name = t.get("theaterName", "Unknown")
                t_id = t.get("theaterId")
                if not t_id:
                    continue

                raw_data = res_obj.get("data")
                err_code = res_obj.get("error")

                if err_code is not None or not raw_data:
                    detailed_logger.error(
                        f"[Thread-{worker_id}] ⚠️ FAILED TO FETCH THEATER MASTER LIST. Status: {err_code} for {t_name}"
                    )
                    continue

                try:
                    movies = raw_data.get("viewModel", {}).get("movies", []) or []
                    matching_movies = [
                        m
                        for m in movies
                        if str(m.get("id", "")).strip() in TARGET_MOVIE_IDS_SET
                    ]
                    if not matching_movies:
                        detailed_logger.info(
                            f"[Thread-{worker_id}] Movie not playing / 0 shows returned for {t_name}"
                        )
                        continue

                    print(f"[Thread-{worker_id}] 🎬 Found shows at: {t_name[:40]}")

                    if t_id not in local_summary_data:
                        local_summary_data[t_id] = {
                            "t_id": t_id,
                            "state": target_state,
                            "name": t_name,
                            "shows": 0,
                            "total": 0,
                            "booked": 0,
                            "gross": 0.0,
                        }

                    grouped_shows = {}
                    for target_movie in matching_movies:
                        m_id = str(target_movie.get("id", "")).strip()
                        m_title = str(target_movie.get("title", "")).strip()

                        for variant in target_movie.get("variants", []) or []:
                            raw_header = variant.get("filmFormatHeader", "") or "Standard"
                            base_format = raw_header.replace(" Format", "").strip()
                            if not base_format:
                                base_format = "Standard"

                            for amenity in variant.get("amenityGroups", []) or []:
                                raw_amenities = [
                                    am.get("name", "").strip()
                                    for am in amenity.get("amenities", [])
                                    if am.get("name")
                                ]
                                raw_amenities_upper = [a.upper() for a in raw_amenities]
                                is_dbox = any(
                                    "D-BOX" in a or "DBOX" in a for a in raw_amenities_upper
                                )

                                show_format = base_format
                                premium_keywords = [
                                    "XD", "IMAX", "LASER AT AMC", "4DX", "RPX", "D-BOX",
                                ]
                                for am_name, am_up in zip(raw_amenities, raw_amenities_upper):
                                    if (
                                        any(k in am_up for k in premium_keywords)
                                        and show_format == base_format
                                    ):
                                        show_format = am_name
                                        break
                                report_format_categories = classify_report_formats(
                                    " ".join([base_format, *raw_amenities]),
                                    base_format,
                                )
                                if is_dbox and report_format_categories == ["Standard"]:
                                    report_format_categories = []

                                show_language = detect_show_language(
                                    raw_amenities, DEFAULT_LANGUAGE, movie_title=m_title
                                )

                                signature_amenities = [
                                    a
                                    for a in raw_amenities_upper
                                    if "D-BOX" not in a
                                    and "DBOX" not in a
                                    and "RESERVED" not in a
                                    and "RECLINERS" not in a
                                    and not re.match(
                                        rf"^\s*(?:{'|'.join(SUPPORTED_LANGUAGES)})\s+(?:WITH\b|LANGUAGE\b)",
                                        a,
                                        re.IGNORECASE,
                                    )
                                ]
                                signature_amenities.sort()
                                room_signature = (
                                    "|".join(signature_amenities)
                                    if signature_amenities
                                    else "STANDARD"
                                )

                                for show in amenity.get("showtimes", []) or []:
                                    show_hash = show.get("showtimeHashCode")
                                    if not show_hash:
                                        continue

                                    show_time = show.get("screenReaderTime", "Unknown")
                                    status = show.get("type", "Unknown")

                                    group_key = (
                                        f"{t_id}_{m_id}_{show_time}_{room_signature}"
                                        if multi_id_mode
                                        else f"{t_id}_{show_time}_{room_signature}"
                                    )

                                    if group_key not in grouped_shows:
                                        grouped_shows[group_key] = {
                                            "state": target_state,
                                            "theater": t_name,
                                            "t_id": t_id,
                                            "time": show_time,
                                            "tiers": [],
                                        }
                                    existing_tier = next(
                                        (
                                            tier
                                            for tier in grouped_shows[group_key]["tiers"]
                                            if tier["hash"] == show_hash
                                        ),
                                        None,
                                    )
                                    if existing_tier:
                                        languages = {
                                            language.strip()
                                            for language in (
                                                existing_tier.get("language", ""),
                                                show_language,
                                            )
                                            if language and language.strip()
                                        }
                                        existing_tier["language"] = " / ".join(
                                            sorted(languages)
                                        )
                                    else:
                                        grouped_shows[group_key]["tiers"].append(
                                            {
                                                "hash": show_hash,
                                                "status": status,
                                                "format": show_format,
                                                "format_categories": report_format_categories,
                                                "language": show_language,
                                                "is_dbox": is_dbox,
                                            }
                                        )
                except Exception as e:
                    detailed_logger.error(
                        f"[Thread-{worker_id}] EXCEPTION parsing theater {t_name}: {e}"
                    )
                    continue

                # --- PHASE 1: COLLECT & FETCH ALL SEAT MAPS FOR THIS THEATER ---
                hashes_to_fetch = []
                for group_key, show_info in grouped_shows.items():
                    for tier in show_info["tiers"]:
                        t_hash = tier["hash"]
                        # Smart Optimization: If already tagged soldout AND in hash cache, skip HTTP call!
                        if (
                            str(tier.get("status", "")).lower() == "soldout"
                            and (t_hash in master_hash_cache or t_hash in local_hash_cache)
                        ):
                            continue
                        hashes_to_fetch.append(t_hash)

                seat_map_results = await fetch_seatmaps_for_theater_js(
                    page,
                    hashes_to_fetch,
                    worker_id,
                    detailed_logger,
                    rate_limit_events,
                )
                reqs_since_refresh += len(hashes_to_fetch)

                for group_key, show_info in grouped_shows.items():
                    show_info["valid_tiers"] = []
                    show_info["failed_tiers"] = []
                    for tier in show_info["tiers"]:
                        tier_hash = tier["hash"]

                        # Fast path for cached SoldOut showtimes
                        if (
                            str(tier.get("status", "")).lower() == "soldout"
                            and tier_hash not in seat_map_results
                            and (tier_hash in master_hash_cache or tier_hash in local_hash_cache)
                        ):
                            tier["status"] = "soldout"
                            show_info["failed_tiers"].append(tier)
                            continue

                        seat_response = seat_map_results.get(tier_hash)
                        if (
                            isinstance(seat_response, list)
                            and seat_response
                            and seat_response[0].get("id") == "PerformanceSoldOut"
                        ):
                            tier["status"] = "soldout"
                            show_info["failed_tiers"].append(tier)
                            detailed_logger.info(
                                f"[{t_id} | {show_info['time']}] Seat map explicitly reports sold out for {tier_hash}."
                            )
                        elif (
                            seat_response
                            and isinstance(seat_response, dict)
                            and "error" not in seat_response
                        ):
                            show_info["valid_tiers"].append(
                                {
                                    "data": seat_response.get("data", seat_response),
                                    "tier_info": tier,
                                }
                            )
                        else:
                            show_info["failed_tiers"].append(tier)

                # --- PHASE 2: PRE-PASS DEAD ROOM DETECTION ---
                aud_booked_sets = {}
                for group_key, show_info in grouped_shows.items():
                    time_booked_seats = {}
                    for saved_tier in show_info["valid_tiers"]:
                        tier_data = saved_tier["data"]
                        aud_id = str(tier_data.get("auditoriumId", "Unknown"))
                        total_dots = len(tier_data.get("seats", []))
                        booked_seats = set()
                        has_front_row = False

                        for s in tier_data.get("seats", []):
                            s_id = str(s.get("id", ""))
                            status = s.get("status", "").upper()
                            row_num = s.get("row", -1)
                            if s_id and status != "A":
                                booked_seats.add(s_id)
                                if row_num == 1 or (
                                    row_num == -1 and str(s_id).upper().startswith("A")
                                ):
                                    has_front_row = True

                        if aud_id not in time_booked_seats:
                            time_booked_seats[aud_id] = {
                                "booked": set(),
                                "total_dots": total_dots,
                                "has_front_row": False,
                            }

                        time_booked_seats[aud_id]["booked"].update(booked_seats)
                        if has_front_row:
                            time_booked_seats[aud_id]["has_front_row"] = True

                    for aud_id, data in time_booked_seats.items():
                        if aud_id not in aud_booked_sets:
                            aud_booked_sets[aud_id] = []
                        aud_booked_sets[aud_id].append(data)

                aud_dead_room_blocks = {}
                for aud_id, shows_list in aud_booked_sets.items():
                    if len(shows_list) >= 2:
                        first_show_booked = shows_list[0]["booked"]
                        first_show_total = shows_list[0]["total_dots"]
                        first_show_has_front = shows_list[0]["has_front_row"]

                        is_exact_match = all(
                            show["booked"] == first_show_booked for show in shows_list
                        )
                        occupancy = (
                            len(first_show_booked) / first_show_total
                            if first_show_total > 0
                            else 0
                        )

                        rule_1 = (
                            is_exact_match
                            and (occupancy < 0.50)
                            and first_show_booked
                            and first_show_has_front
                        )
                        rule_2 = (
                            is_exact_match and (occupancy < 0.30) and first_show_booked
                        )

                        if rule_1 or rule_2:
                            aud_dead_room_blocks[aud_id] = first_show_booked
                            matched_rule = (
                                "Rule 1 (<50% + FrontRow)"
                                if rule_1
                                else "Rule 2 (<30% Strict)"
                            )
                            detailed_logger.info(
                                f"[{t_id} | Aud {aud_id}] 🛑 DEAD ROOM DETECTED ({matched_rule}): "
                                f"{len(shows_list)} unique shows have the exact same "
                                f"{len(first_show_booked)} structural blocks."
                            )
                        else:
                            aud_dead_room_blocks[aud_id] = set()
                    else:
                        aud_dead_room_blocks[aud_id] = set()

                # --- PHASE 3: PROCESS EACH SHOW (EXACT MATRIX & PHANTOM MATH) ---
                combined_cache_lookup = {**master_hash_cache, **local_hash_cache}

                for group_idx, (group_key, show_info) in enumerate(
                    grouped_shows.items(), 1
                ):
                    try:
                        show_time = show_info["time"]
                        valid_tiers = show_info["valid_tiers"]
                        failed_tiers = show_info["failed_tiers"]

                        # 🚨 MATRIX SCENARIOS 9 & 10: ALL APIs FAILED
                        if len(valid_tiers) == 0:
                            has_generic_error = any(
                                ft.get("status", "").lower() != "soldout"
                                for ft in failed_tiers
                            )

                            # ✨ HASH CACHE SELF-HEALING (All Tiers Failed)
                            if len(failed_tiers) > 0 and not has_generic_error:
                                base_failed = [
                                    ft
                                    for ft in failed_tiers
                                    if not ft.get("is_dbox", False)
                                ]
                                dbox_failed = [
                                    ft for ft in failed_tiers if ft.get("is_dbox", False)
                                ]

                                if not base_failed:
                                    base_failed = dbox_failed
                                    dbox_failed = []

                                for base_tier in base_failed:
                                    paired_tiers = [base_tier]
                                    if dbox_failed:
                                        paired_tiers.append(dbox_failed.pop(0))

                                    c_total = 0
                                    c_booked = 0
                                    c_gross = 0.0
                                    c_prices = set()
                                    pair_cached = True

                                    for paired_tier in paired_tiers:
                                        tier_hash = paired_tier.get("hash")
                                        if tier_hash not in combined_cache_lookup:
                                            pair_cached = False
                                            break

                                        cached = combined_cache_lookup[tier_hash]
                                        capacity = cached["capacity"]
                                        max_gross = cached["max_gross"]
                                        if max_gross == 0 and capacity > 0:
                                            max_gross = capacity * MAX_TIER_PRICE

                                        c_total += capacity
                                        if (
                                            paired_tier.get("status", "").lower()
                                            == "soldout"
                                        ):
                                            c_booked += capacity
                                            c_gross += max_gross
                                            if capacity > 0:
                                                c_prices.add(max_gross / capacity)

                                    pair_urls = [
                                        f"https://www.fandango.com/napi/seatMap/{pt['hash']}"
                                        for pt in paired_tiers
                                        if pt.get("hash")
                                    ]
                                    if not pair_cached:
                                        for paired_tier in paired_tiers:
                                            local_sold_out_queue.append(
                                                {
                                                    "state": target_state,
                                                    "t_id": t_id,
                                                    "theater": t_name,
                                                    "format": paired_tier["format"],
                                                    "format_categories": paired_tier.get(
                                                        "format_categories", []
                                                    ),
                                                    "language": paired_tier.get(
                                                        "language", DEFAULT_LANGUAGE
                                                    ),
                                                    "time": show_time,
                                                    "seat_map_urls": (
                                                        f"https://www.fandango.com/napi/seatMap/{paired_tier.get('hash', '')}"
                                                    ),
                                                }
                                            )
                                        continue

                                    final_format = " / ".join(
                                        sorted({pt["format"] for pt in paired_tiers})
                                    )
                                    final_language = " / ".join(
                                        sorted(
                                            {
                                                pt.get("language", DEFAULT_LANGUAGE)
                                                for pt in paired_tiers
                                            }
                                        )
                                    )
                                    urls_str = " | ".join(pair_urls)
                                    price_str = (
                                        " / ".join(
                                            sorted([f"${p:.2f}" for p in c_prices])
                                        )
                                        if c_prices
                                        else "$0.00"
                                    )
                                    final_status = (
                                        "Sold Out"
                                        if c_booked >= c_total and c_total > 0
                                        else "Available"
                                    )

                                    local_shows_data.append(
                                        {
                                            "state": target_state,
                                            "t_id": t_id,
                                            "theater": t_name,
                                            "format": final_format,
                                            "language": final_language,
                                            "time": show_time,
                                            "status": final_status,
                                            "format_categories": combine_report_format_categories(
                                                paired_tiers
                                            ),
                                            "price_str": price_str,
                                            "total": c_total,
                                            "booked": c_booked,
                                            "gross": c_gross,
                                            "seat_map_urls": urls_str,
                                        }
                                    )
                                    local_summary_data[t_id]["shows"] += 1
                                    local_summary_data[t_id]["total"] += c_total
                                    local_summary_data[t_id]["booked"] += c_booked
                                    local_summary_data[t_id]["gross"] += c_gross
                                    detailed_logger.info(
                                        f"[{t_id} | {show_time}] SELF-HEALED (All-Failed) via Hash Cache. "
                                        f"Booked: {c_booked}/{c_total}"
                                    )
                                    local_hash_healed_log.append(
                                        f"( {target_state} ) {t_name} - {show_time} [{final_format}] "
                                        f"- Healed {c_total} seats | ${c_gross:,.2f}"
                                    )

                                continue

                            if has_generic_error:
                                formats_seen = list(
                                    set(ft["format"] for ft in failed_tiers)
                                )
                                final_format = (
                                    " / ".join(sorted(formats_seen))
                                    if formats_seen
                                    else "Standard"
                                )
                                languages_seen = list(
                                    set(
                                        ft.get("language", DEFAULT_LANGUAGE)
                                        for ft in failed_tiers
                                    )
                                )
                                final_language = (
                                    " / ".join(sorted(languages_seen))
                                    if languages_seen
                                    else DEFAULT_LANGUAGE
                                )
                                failed_urls = [
                                    f"https://www.fandango.com/napi/seatMap/{ft['hash']}"
                                    for ft in failed_tiers
                                    if ft.get("hash")
                                ]
                                urls_str = " | ".join(failed_urls)

                                url_log = f" URL(s): {urls_str}" if urls_str else ""
                                log_msg = (
                                    f"( {target_state} ) {t_name} - {show_time} [{final_format}] - "
                                    f"This show is available but not able to fetch seats.{url_log}"
                                )
                                detailed_logger.info(
                                    f"[{t_id} | {show_time}] Matrix S10: ALL APIs FAILED + 'Available'. {log_msg}"
                                )
                                local_ignored_shows_log.append(log_msg)

                                local_shows_data.append(
                                    {
                                        "state": target_state,
                                        "t_id": t_id,
                                        "theater": t_name,
                                        "format": final_format,
                                        "language": final_language,
                                        "time": show_time,
                                        "status": "Available (Map Error)",
                                        "format_categories": combine_report_format_categories(
                                            failed_tiers
                                        ),
                                        "price_str": "$0.00",
                                        "total": 0,
                                        "booked": 0,
                                        "gross": 0.0,
                                        "seat_map_urls": urls_str,
                                    }
                                )

                                local_summary_data[t_id]["shows"] += 1
                                continue
                            else:
                                for failed_tier in failed_tiers:
                                    local_sold_out_queue.append(
                                        {
                                            "state": target_state,
                                            "t_id": t_id,
                                            "theater": t_name,
                                            "format": failed_tier["format"],
                                            "format_categories": failed_tier.get(
                                                "format_categories", []
                                            ),
                                            "language": failed_tier.get(
                                                "language", DEFAULT_LANGUAGE
                                            ),
                                            "time": show_time,
                                            "seat_map_urls": (
                                                f"https://www.fandango.com/napi/seatMap/{failed_tier.get('hash', '')}"
                                            ),
                                        }
                                    )
                                continue

                        # 🧠 PHYSICAL OVERLAP CLUSTERING
                        clusters = []

                        def get_available_seat_ids(s_array):
                            return set(
                                s.get("id")
                                for s in s_array
                                if s.get("id") and s.get("status") == "A"
                            )

                        for vt in valid_tiers:
                            seats_array = vt["data"].get("seats", [])
                            seat_ids = set(
                                s.get("id") for s in seats_array if s.get("id")
                            )
                            vt_available_seats = get_available_seat_ids(seats_array)

                            matched = False
                            for cluster in clusters:
                                vt_svg = vt["data"].get("backgroundSvg", "")
                                cluster_svg = cluster["tiers"][0]["data"].get(
                                    "backgroundSvg", ""
                                )

                                has_intersection = bool(
                                    seat_ids
                                    and cluster["seat_ids"].intersection(seat_ids)
                                )
                                has_same_svg = bool(
                                    vt_svg and cluster_svg and vt_svg == cluster_svg
                                )

                                if has_intersection or has_same_svg:
                                    has_collision = False
                                    for existing_vt in cluster["tiers"]:
                                        existing_available_seats = (
                                            get_available_seat_ids(
                                                existing_vt["data"].get("seats", [])
                                            )
                                        )
                                        if vt_available_seats.intersection(
                                            existing_available_seats
                                        ):
                                            has_collision = True
                                            break

                                    if not has_collision:
                                        cluster["tiers"].append(vt)
                                        cluster["seat_ids"].update(seat_ids)
                                        matched = True
                                        break

                            if not matched:
                                clusters.append(
                                    {
                                        "seat_ids": seat_ids,
                                        "tiers": [vt],
                                        "failed_tiers": [],
                                    }
                                )

                        failed_bases = [
                            ft for ft in failed_tiers if not ft.get("is_dbox")
                        ]
                        failed_dboxes = [
                            ft for ft in failed_tiers if ft.get("is_dbox")
                        ]

                        for failed_dbox in failed_dboxes:
                            for cluster in clusters:
                                cluster_has_dbox = any(
                                    t["tier_info"].get("is_dbox")
                                    for t in cluster["tiers"]
                                ) or any(
                                    t.get("is_dbox") for t in cluster["failed_tiers"]
                                )
                                if not cluster_has_dbox:
                                    cluster["failed_tiers"].append(failed_dbox)
                                    break
                            else:
                                clusters.append(
                                    {
                                        "seat_ids": set(),
                                        "tiers": [],
                                        "failed_tiers": [failed_dbox],
                                    }
                                )

                        for failed_base in failed_bases:
                            for cluster in clusters:
                                cluster_has_base = any(
                                    not t["tier_info"].get("is_dbox")
                                    for t in cluster["tiers"]
                                ) or any(
                                    not t.get("is_dbox")
                                    for t in cluster["failed_tiers"]
                                )
                                if not cluster_has_base:
                                    cluster["failed_tiers"].append(failed_base)
                                    break
                            else:
                                clusters.append(
                                    {
                                        "seat_ids": set(),
                                        "tiers": [],
                                        "failed_tiers": [failed_base],
                                    }
                                )

                        # 🎬 THE MASTER MATRIX (UNIFIED ALLOCATION ALGORITHM)
                        for c_idx, cluster in enumerate(clusters):
                            combined_total = combined_booked = 0
                            combined_gross = 0.0
                            prices_seen = set()
                            formats_seen = set()
                            languages_seen = set()
                            matrix_urls = []

                            for vt in cluster["tiers"]:
                                formats_seen.add(vt["tier_info"]["format"])
                                languages_seen.add(vt["tier_info"]["language"])
                                if vt["tier_info"].get("hash"):
                                    matrix_urls.append(
                                        f"https://www.fandango.com/napi/seatMap/{vt['tier_info']['hash']}"
                                    )
                            for ft in cluster["failed_tiers"]:
                                formats_seen.add(ft["format"])
                                if "language" in ft:
                                    languages_seen.add(ft["language"])
                                if ft.get("hash"):
                                    matrix_urls.append(
                                        f"https://www.fandango.com/napi/seatMap/{ft['hash']}"
                                    )

                            matrix_urls_str = " | ".join(matrix_urls)
                            final_format = " / ".join(sorted(formats_seen))
                            final_language = (
                                " / ".join(sorted(languages_seen))
                                if languages_seen
                                else DEFAULT_LANGUAGE
                            )

                            uid_log = f"[{t_id} | {show_time} | {final_format}]"
                            is_multi_tier = (
                                len(cluster["tiers"]) + len(cluster["failed_tiers"])
                            ) > 1

                            healed_tiers_info = []

                            if not cluster["tiers"]:
                                has_available_failed_tiers = any(
                                    failed_tier.get("status", "").lower() != "soldout"
                                    for failed_tier in cluster["failed_tiers"]
                                )
                                for failed_tier in cluster["failed_tiers"]:
                                    tier_hash = failed_tier.get("hash")
                                    is_soldout = (
                                        failed_tier.get("status", "").lower()
                                        == "soldout"
                                    )
                                    if tier_hash in combined_cache_lookup:
                                        cached = combined_cache_lookup[tier_hash]
                                        capacity = cached["capacity"]
                                        max_gross = cached["max_gross"]
                                        if max_gross == 0 and capacity > 0:
                                            max_gross = capacity * MAX_TIER_PRICE
                                        combined_total += capacity
                                        if is_soldout:
                                            combined_booked += capacity
                                            combined_gross += max_gross
                                            if capacity > 0:
                                                prices_seen.add(max_gross / capacity)
                                        calc_method_log = "Standalone: Hash Healed"
                                        healed_tiers_info.append(
                                            f"[{failed_tier['format']}] {capacity} seats | ${max_gross:,.2f}"
                                        )
                                    elif is_soldout:
                                        local_sold_out_queue.append(
                                            {
                                                "state": target_state,
                                                "t_id": t_id,
                                                "theater": t_name,
                                                "format": failed_tier["format"],
                                                "format_categories": failed_tier.get(
                                                    "format_categories", []
                                                ),
                                                "language": failed_tier.get(
                                                    "language", DEFAULT_LANGUAGE
                                                ),
                                                "time": show_time,
                                                "seat_map_urls": f"https://www.fandango.com/napi/seatMap/{tier_hash or ''}",
                                            }
                                        )

                                if combined_total == 0:
                                    continue

                                if combined_total > 0:
                                    kb_key = f"{t_id}_{final_format}"
                                    if kb_key not in local_knowledge_base:
                                        local_knowledge_base[kb_key] = {
                                            "total_seats_sum": 0,
                                            "count": 0,
                                            "prices": set(),
                                        }
                                    local_knowledge_base[kb_key][
                                        "total_seats_sum"
                                    ] += combined_total
                                    local_knowledge_base[kb_key]["count"] += 1
                                    local_knowledge_base[kb_key]["prices"].update(
                                        prices_seen
                                    )

                                final_status = _get_show_status(
                                    combined_booked,
                                    combined_total,
                                    has_available_failed_tiers,
                                )
                                price_str = (
                                    " / ".join(
                                        sorted(f"${price:.2f}" for price in prices_seen)
                                    )
                                    if prices_seen
                                    else "$0.00"
                                )
                                local_hash_healed_log.append(
                                    f"( {target_state} ) {t_name} - {show_time} [{final_format}] "
                                    f"- Healed {combined_total} seats | ${combined_gross:,.2f}"
                                )
                                local_shows_data.append(
                                    {
                                        "state": target_state,
                                        "t_id": t_id,
                                        "theater": t_name,
                                        "format": final_format,
                                        "language": final_language,
                                        "time": show_time,
                                        "status": final_status,
                                        "format_categories": combine_report_format_categories(
                                            [
                                                ft["tier_info"]
                                                for ft in cluster["tiers"]
                                            ]
                                            + cluster["failed_tiers"]
                                        ),
                                        "price_str": price_str,
                                        "total": combined_total,
                                        "booked": combined_booked,
                                        "gross": combined_gross,
                                        "seat_map_urls": matrix_urls_str,
                                    }
                                )
                                local_summary_data[t_id]["shows"] += 1
                                local_summary_data[t_id]["total"] += combined_total
                                local_summary_data[t_id]["booked"] += combined_booked
                                local_summary_data[t_id]["gross"] += combined_gross
                                continue

                            base_vt = None
                            valid_base_tiers = [
                                vt
                                for vt in cluster["tiers"]
                                if not vt["tier_info"].get("is_dbox")
                            ]
                            if valid_base_tiers:
                                valid_base_tiers.sort(
                                    key=lambda vt: int(
                                        vt["data"]
                                        .get("areas", [{}])[0]
                                        .get("totalSeatCount", 0)
                                    )
                                    if vt.get("data", {}).get("areas")
                                    else 0,
                                    reverse=True,
                                )
                                base_vt = valid_base_tiers[0]
                            elif cluster["tiers"]:
                                base_vt = cluster["tiers"][0]

                            safe_avg_price = AVG_PRICE
                            if PRICE_TAX_CUT and safe_avg_price > 0:
                                safe_avg_price = float((safe_avg_price // 5) * 5)

                            base_seats = base_vt["data"].get("seats", [])
                            base_phys_total_dots = sum(
                                1
                                for s in base_seats
                                if s.get("status", "").upper() in ["A", "R"]
                            )
                            base_has_valid_area = any(
                                s.get("areaId") for s in base_seats
                            )

                            # --- 🌟 THE PHANTOM SEAT SUBTRACTOR (AREA-BY-AREA) 🌟 ---
                            allocated_cap = 0
                            allocated_booked = 0
                            allocated_gross = 0.0

                            for vt in cluster["tiers"]:
                                vt_seats = vt["data"].get("seats", [])
                                aud_id = str(vt["data"].get("auditoriumId", "Unknown"))
                                dead_room_seats = aud_dead_room_blocks.get(
                                    aud_id, set()
                                )

                                area_stats = {}
                                for a in vt["data"].get("areas", []):
                                    a_id = str(a.get("id", ""))
                                    try:
                                        p = float(
                                            a.get("ticketInfo", [{}])[0].get(
                                                "price", 0
                                            )
                                        )
                                        if PRICE_TAX_CUT and p > 0:
                                            p = float((p // 5) * 5)
                                    except Exception:
                                        p = 0.0

                                    area_stats[a_id] = {
                                        "cache_cap": int(a.get("totalSeatCount", 0)),
                                        "cache_avail": int(
                                            a.get("availableSeatCount", 0)
                                        ),
                                        "price": p,
                                        "phys_total": 0,
                                        "raw_booked": 0,
                                        "explicit_blocks": 0,
                                        "front_row_blocks": 0,
                                        "explicit_in_front_row": 0,
                                        "dead_room_blocks": 0,
                                    }

                                if vt_seats:
                                    temp_phys_total = sum(
                                        1
                                        for s in vt_seats
                                        if s.get("status", "").upper() in ["A", "R"]
                                    )
                                    temp_phys_booked = sum(
                                        1
                                        for s in vt_seats
                                        if s.get("status", "").upper() == "R"
                                    )
                                    occupancy = (
                                        (temp_phys_booked / temp_phys_total)
                                        if temp_phys_total > 0
                                        else 0.0
                                    )

                                    for s in vt_seats:
                                        status = s.get("status", "").upper()
                                        s_id = s.get("id", "")
                                        row_num = s.get("row", -1)
                                        a_id = str(s.get("areaId", ""))

                                        if a_id not in area_stats:
                                            first_a_id = (
                                                list(area_stats.keys())[0]
                                                if area_stats
                                                else "UNKNOWN"
                                            )
                                            if first_a_id not in area_stats:
                                                area_stats[first_a_id] = {
                                                    "cache_cap": 0,
                                                    "cache_avail": 0,
                                                    "price": 0.0,
                                                    "phys_total": 0,
                                                    "raw_booked": 0,
                                                    "explicit_blocks": 0,
                                                    "front_row_blocks": 0,
                                                    "explicit_in_front_row": 0,
                                                    "dead_room_blocks": 0,
                                                }
                                            a_id = first_a_id

                                        ast = area_stats[a_id]
                                        ast["phys_total"] += 1

                                        if status != "A":
                                            ast["raw_booked"] += 1

                                            is_explicit = status != "R"
                                            is_front_row = row_num == 1 or (
                                                row_num == -1
                                                and str(s_id).upper().startswith("A")
                                            )

                                            if is_explicit:
                                                ast["explicit_blocks"] += 1
                                            if (
                                                is_front_row
                                                and occupancy < OCC_THRESHOLD_FRONTROW
                                            ):
                                                ast["front_row_blocks"] += 1
                                            if (
                                                is_explicit
                                                and is_front_row
                                                and occupancy < OCC_THRESHOLD_FRONTROW
                                            ):
                                                ast["explicit_in_front_row"] += 1
                                            if s_id in dead_room_seats:
                                                ast["dead_room_blocks"] += 1

                                real_t_id = vt["data"].get("theaterId", t_id)
                                screen_id = f"{real_t_id}_{aud_id}"

                                force_cached = screen_id in PREFER_CACHED_SCREENS
                                force_physical = screen_id in PREFER_PHYSICAL_SCREENS

                                vt_true_cap = 0
                                vt_true_booked = 0
                                vt_gross = 0.0
                                vt_max_gross = 0.0
                                vt_phys_total = 0
                                vt_raw_booked = 0
                                seats_to_remove = 0

                                if (
                                    SEAT_COUNT_MODE == 1
                                    and vt_seats
                                    and not force_cached
                                ) or force_physical:
                                    for a_id, ast in area_stats.items():
                                        a_cache_cap = ast["cache_cap"]
                                        a_price = ast["price"]

                                        a_phantom_math = (
                                            max(0, ast["phys_total"] - a_cache_cap)
                                            if a_cache_cap > 0
                                            else 0
                                        )
                                        a_visual_blocks = (
                                            ast["explicit_blocks"]
                                            + ast["front_row_blocks"]
                                        ) - ast["explicit_in_front_row"]
                                        a_seats_to_remove = max(
                                            a_phantom_math,
                                            a_visual_blocks,
                                            ast["dead_room_blocks"],
                                        )

                                        a_true_cap = a_cache_cap
                                        a_true_booked = max(
                                            0, ast["raw_booked"] - a_seats_to_remove
                                        )

                                        vt_phys_total += ast["phys_total"]
                                        vt_raw_booked += ast["raw_booked"]
                                        seats_to_remove += a_seats_to_remove

                                        vt_true_cap += a_true_cap
                                        vt_true_booked += a_true_booked
                                        vt_gross += a_true_booked * a_price
                                        vt_max_gross += a_true_cap * a_price
                                        if a_price > 0:
                                            prices_seen.add(a_price)
                                else:
                                    for a_id, ast in area_stats.items():
                                        a_cache_cap = ast["cache_cap"]
                                        a_price = ast["price"]

                                        a_true_cap = a_cache_cap
                                        a_true_booked = max(
                                            0, a_cache_cap - ast["cache_avail"]
                                        )

                                        vt_true_cap += a_true_cap
                                        vt_true_booked += a_true_booked
                                        vt_gross += a_true_booked * a_price
                                        vt_max_gross += a_true_cap * a_price
                                        if a_price > 0:
                                            prices_seen.add(a_price)

                                if (
                                    vt["tier_info"].get("status", "").lower()
                                    == "soldout"
                                ):
                                    vt_true_cap = 0
                                    vt_true_booked = 0
                                    vt_gross = 0.0
                                    vt_max_gross = 0.0
                                    for a_id, ast in area_stats.items():
                                        vt_true_cap += ast["cache_cap"]
                                        vt_true_booked += ast["cache_cap"]
                                        vt_gross += ast["cache_cap"] * ast["price"]
                                        vt_max_gross += ast["cache_cap"] * ast["price"]

                                vt["true_capacity"] = vt_true_cap
                                vt["true_booked"] = vt_true_booked

                                allocated_cap += vt_true_cap
                                allocated_booked += vt_true_booked
                                allocated_gross += vt_gross

                                tier_hash = vt["tier_info"].get("hash")
                                if tier_hash and vt_true_cap > 0:
                                    learned_entry = {
                                        "capacity": vt_true_cap,
                                        "max_gross": vt_max_gross,
                                        "format": vt["tier_info"].get(
                                            "format", "Standard"
                                        ),
                                    }
                                    local_hash_cache[tier_hash] = learned_entry
                                    combined_cache_lookup[tier_hash] = learned_entry

                            all_tiers_info = [
                                vt["tier_info"] for vt in cluster["tiers"]
                            ] + cluster["failed_tiers"]
                            is_any_available = any(
                                t.get("status", "").lower() != "soldout"
                                for t in all_tiers_info
                            )
                            has_failed_tiers = len(cluster["failed_tiers"]) > 0
                            has_available_failed_tiers = any(
                                ft.get("status", "").lower() != "soldout"
                                for ft in cluster["failed_tiers"]
                            )

                            base_aud_id = base_vt["data"].get("auditoriumId", "Unknown")
                            real_base_t_id = base_vt["data"].get("theaterId", t_id)
                            base_screen_id = f"{real_base_t_id}_{base_aud_id}"
                            is_forced_cached = base_screen_id in PREFER_CACHED_SCREENS

                            if (
                                not is_multi_tier
                                and SEAT_COUNT_MODE == 1
                                and base_seats
                                and base_has_valid_area
                                and not is_forced_cached
                            ):
                                combined_total = allocated_cap
                                combined_booked = allocated_booked
                                combined_gross = allocated_gross
                                calc_method_log = "Physical Catch-All"
                            else:
                                if not has_failed_tiers:
                                    combined_total = allocated_cap
                                    combined_booked = allocated_booked
                                    combined_gross = allocated_gross
                                    calc_method_log = "Per-Tier Phantom Sync"
                                else:
                                    missing_capacity = max(
                                        0, base_phys_total_dots - allocated_cap
                                    )
                                    combined_total = allocated_cap
                                    combined_booked = allocated_booked
                                    combined_gross = allocated_gross

                                    for ft in cluster["failed_tiers"]:
                                        tier_hash = ft.get("hash")
                                        is_soldout = (
                                            ft.get("status", "").lower() == "soldout"
                                        )

                                        if tier_hash in combined_cache_lookup:
                                            c_cap = combined_cache_lookup[tier_hash][
                                                "capacity"
                                            ]
                                            c_gross = combined_cache_lookup[tier_hash][
                                                "max_gross"
                                            ]
                                            if c_gross == 0 and c_cap > 0:
                                                c_gross = c_cap * MAX_TIER_PRICE

                                            combined_total += c_cap
                                            if is_soldout:
                                                combined_booked += c_cap
                                                combined_gross += c_gross
                                                if c_cap > 0:
                                                    prices_seen.add(c_gross / c_cap)

                                            missing_capacity = max(
                                                0, missing_capacity - c_cap
                                            )
                                            calc_method_log = "Matrix: Hash Healed"
                                            healed_tiers_info.append(
                                                f"[{ft['format']}] {c_cap} seats | ${c_gross:,.2f}"
                                            )
                                        else:
                                            combined_total += missing_capacity
                                            if is_soldout:
                                                missing_booked = missing_capacity
                                                combined_booked += missing_booked
                                                price_to_use = (
                                                    safe_avg_price
                                                    if not is_any_available
                                                    else MAX_TIER_PRICE
                                                )
                                                combined_gross += (
                                                    missing_booked * price_to_use
                                                )
                                                if price_to_use > 0:
                                                    prices_seen.add(price_to_use)

                                            missing_capacity = 0
                                            calc_method_log = "Matrix: Math Fallback"

                            if combined_total > 0:
                                kb_key = f"{t_id}_{final_format}"
                                if kb_key not in local_knowledge_base:
                                    local_knowledge_base[kb_key] = {
                                        "total_seats_sum": 0,
                                        "count": 0,
                                        "prices": set(),
                                    }
                                local_knowledge_base[kb_key][
                                    "total_seats_sum"
                                ] += combined_total
                                local_knowledge_base[kb_key]["count"] += 1
                                for p in prices_seen:
                                    if p > 0:
                                        local_knowledge_base[kb_key]["prices"].add(p)

                            final_status = _get_show_status(
                                combined_booked,
                                combined_total,
                                has_available_failed_tiers,
                            )
                            price_str = (
                                " / ".join(sorted([f"${p:.2f}" for p in prices_seen]))
                                if prices_seen
                                else "$0.00"
                            )

                            if "Hash Healed" in calc_method_log:
                                if "Matrix" in calc_method_log and healed_tiers_info:
                                    partial_details = " + ".join(healed_tiers_info)
                                    local_hash_healed_log.append(
                                        f"( {target_state} ) {t_name} - {show_time} - PARTIAL HEAL: "
                                        f"{partial_details} (Merged into live room)"
                                    )
                                else:
                                    local_hash_healed_log.append(
                                        f"( {target_state} ) {t_name} - {show_time} [{final_format}] "
                                        f"- Healed {combined_total} seats | ${combined_gross:,.2f}"
                                    )

                            local_shows_data.append(
                                {
                                    "state": target_state,
                                    "t_id": t_id,
                                    "theater": t_name,
                                    "format": final_format,
                                    "language": final_language,
                                    "time": show_time,
                                    "status": final_status,
                                    "format_categories": combine_report_format_categories(
                                        [
                                            vt["tier_info"]
                                            for vt in cluster["tiers"]
                                        ]
                                        + cluster["failed_tiers"]
                                    ),
                                    "price_str": price_str,
                                    "total": combined_total,
                                    "booked": combined_booked,
                                    "gross": combined_gross,
                                    "seat_map_urls": matrix_urls_str,
                                }
                            )

                            local_summary_data[t_id]["shows"] += 1
                            local_summary_data[t_id]["total"] += combined_total
                            local_summary_data[t_id]["booked"] += combined_booked
                            local_summary_data[t_id]["gross"] += combined_gross

                    except Exception as e:
                        detailed_logger.error(
                            f"[Thread-{worker_id}] EXCEPTION processing cluster at {show_time}: {e}"
                        )

        print(f"[Thread-{worker_id}] 🛑 Queue Empty. Closing browser.")
        await browser.close()

    return {
        "shows_data": local_shows_data,
        "summary_data": local_summary_data,
        "knowledge_base": local_knowledge_base,
        "sold_out_queue": local_sold_out_queue,
        "ignored_log": local_ignored_shows_log,
        "hash_cache": local_hash_cache,
        "hash_healed_log": local_hash_healed_log,
    }


async def run_all_workers_async(
    tasks_list, total_tasks, detailed_logger, master_hash_cache, rate_limit_events
):
    async_queue = asyncio.Queue()
    for item in tasks_list:
        async_queue.put_nowait(item)

    worker_coros = [
        process_theaters_worker_async(
            async_queue,
            i + 1,
            total_tasks,
            detailed_logger,
            master_hash_cache,
            rate_limit_events,
        )
        for i in range(MAX_WORKERS)
    ]
    return await asyncio.gather(*worker_coros, return_exceptions=True)


# =============================================================================
# ── MAIN EXECUTION ───────────────────────────────────────────────────────────
# =============================================================================

if __name__ == "__main__":
    PIPELINE_START_TIME = time.time()

    # ✨ SETUP STEALTH FILE LOGGER (No Terminal Output)
    os.makedirs("logs", exist_ok=True)
    log_timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_filename = f"logs/detailed_logs_{log_timestamp}.log"

    detailed_logger = logging.getLogger("FandangoDetailedLogger")
    detailed_logger.setLevel(logging.INFO)
    fh = logging.FileHandler(log_filename, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    detailed_logger.addHandler(fh)
    detailed_logger.propagate = False

    ids_display = ", ".join(TARGET_MOVIE_IDS)
    detailed_logger.info(
        f"🚀 MULTI-BROWSER PIPELINE STARTED: {SHOW_DATE} | Movie: {MOVIE_TITLE} ({ids_display}) | Workers: {MAX_WORKERS}"
    )
    print(
        f"🚀 HIGH-SPEED PIPELINE STARTED | Movie: {MOVIE_TITLE} ({ids_display}) | Date: {SHOW_DATE} | Workers: {MAX_WORKERS}"
    )

    if not os.path.exists(MAPPING_FILE):
        print(
            f"❌ Error: {MAPPING_FILE} not found. Please place {MAPPING_FILE} in the project folder!"
        )
        exit()

    try:
        with open(MAPPING_FILE, "r", encoding="utf-8") as f:
            master_map = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Error: {MAPPING_FILE} is corrupted or invalid JSON. ({e})")
        exit()

    # 1. FLATTEN TASK LIST (Theater-by-Theater Load Balancing)
    tasks_list = []
    task_counter = 1

    for state in TARGET_STATES:
        if state in master_map:
            for theater in master_map[state]:
                tasks_list.append((task_counter, state, theater))
                task_counter += 1

    total_tasks = len(tasks_list)
    print(
        f"📦 Total Theaters to process: {total_tasks} across {len(TARGET_STATES)} States."
    )

    # 2. MASTER DATA STORES
    master_shows_data = []
    master_summary_data = {}
    master_knowledge_base = {}
    master_sold_out_queue = []
    master_ignored_shows_log = []
    master_rate_limit_shows = []
    master_hash_healed_log = []

    master_hash_cache = {}
    new_hash_entries = {}

    # =========================================================================
    # ── 2.5 LOAD LOCAL & FIREBASE HASH CACHE ─────────────────────────────────
    # =========================================================================
    local_hash_cache = {}
    if os.path.exists(HASH_CACHE_FILE):
        try:
            with open(HASH_CACHE_FILE, "r", encoding="utf-8") as f:
                local_hash_cache = json.load(f)
            print(
                f"📈 Loaded {len(local_hash_cache)} historical tier fingerprints from local cache."
            )
        except Exception as e:
            print(f"⚠️ Failed to load local hash cache: {e}")
    else:
        print("ℹ️ No historical hash cache found locally.")

    firebase_hash_cache = load_firebase_hash_cache() if FIREBASE_READY else {}
    master_hash_cache = merge_hash_caches(local_hash_cache, firebase_hash_cache)

    recent_shows_data = []
    recent_shows_timestamp = None
    active_run_id = None
    daily_snapshot_id = None
    daily_snapshot_observed_at = None
    daily_baseline_rows = []
    if FIREBASE_READY:
        date_root = firebase_movie_root()
        print("\n📡 Fetching the published V2 pointers and compact daily baseline...")
        published = db.reference(f"{date_root}/published").get() or {}
        active_run_id = published.get("active_run_id")
        if active_run_id:
            detail_page_count = int(
                published.get(
                    "active_detail_page_count",
                    published.get("detail_page_count", 0),
                )
                or 0
            )
            recent_shows_data = load_v2_show_pages(
                date_root,
                active_run_id,
                detail_page_count,
            )
            previous_summary = (
                db.reference(
                    f"{date_root}/analytics_v2/runs/{active_run_id}/summary/metrics"
                ).get()
                or {}
            )
            append_unpublished_extra_metrics(
                recent_shows_data,
                previous_summary,
            )
            recent_shows_timestamp = published.get("observed_at")
            print(
                f"📈 Loaded {len(recent_shows_data)} shows from V2 run {active_run_id}."
            )
        else:
            print("ℹ️ No published V2 run exists yet.")

        daily_baseline = (
            db.reference(f"{date_root}/comparison_baselines/daily").get() or {}
        )
        daily_snapshot_id = daily_baseline.get("run_id")
        baseline_shows = daily_baseline.get("shows")
        if isinstance(baseline_shows, dict):
            daily_baseline_rows = [
                {**row, "_analytics_show_id": show_id}
                for show_id, row in baseline_shows.items()
                if isinstance(row, dict)
            ]
            daily_baseline_rows.extend(
                row
                for row in daily_baseline.get("extras", [])
                if isinstance(row, dict)
            )
        else:
            daily_baseline_rows = [
                {**row, "_analytics_show_id": key}
                for key, row in daily_baseline.items()
                if key not in {"run_id", "observed_at", "extras"}
                and isinstance(row, dict)
            ]
        if daily_baseline_rows and daily_snapshot_id:
            daily_history_entry = (
                db.reference(f"{date_root}/history/{daily_snapshot_id}").get() or {}
            )
            if (
                "total_gross" in daily_history_entry
                and "booked_tickets" in daily_history_entry
            ):
                append_unpublished_extra_metrics(
                    daily_baseline_rows,
                    {
                        "booked_gross": daily_history_entry.get("total_gross"),
                        "booked_tickets": daily_history_entry.get("booked_tickets"),
                        "capacity_seats": sum(
                            float(row.get("total") or 0) for row in daily_baseline_rows
                        ),
                    },
                )
            daily_snapshot_observed_at = daily_baseline.get("observed_at")
            print(f"📈 Loaded V2 daily baseline {daily_snapshot_id}.")
        else:
            print("ℹ️ No manually selected daily baseline exists yet.")

    # 3. EXECUTE HIGH-SPEED ASYNC MULTI-BROWSER POOL
    try:
        worker_results = asyncio.run(
            run_all_workers_async(
                tasks_list,
                total_tasks,
                detailed_logger,
                master_hash_cache,
                master_rate_limit_shows,
            )
        )
        for result in worker_results:
            if isinstance(result, Exception):
                print(f"❌ A worker encountered an error: {result}")
                continue
            if not isinstance(result, dict):
                continue

            master_shows_data.extend(result["shows_data"])
            master_sold_out_queue.extend(result["sold_out_queue"])
            master_ignored_shows_log.extend(result["ignored_log"])
            master_hash_healed_log.extend(result.get("hash_healed_log", []))

            for t_id, data in result["summary_data"].items():
                if t_id not in master_summary_data:
                    master_summary_data[t_id] = data
                else:
                    master_summary_data[t_id]["shows"] += data["shows"]
                    master_summary_data[t_id]["total"] += data["total"]
                    master_summary_data[t_id]["booked"] += data["booked"]
                    master_summary_data[t_id]["gross"] += data["gross"]

            for kb_key, kb_data in result["knowledge_base"].items():
                if kb_key not in master_knowledge_base:
                    master_knowledge_base[kb_key] = kb_data
                else:
                    master_knowledge_base[kb_key]["total_seats_sum"] += kb_data[
                        "total_seats_sum"
                    ]
                    master_knowledge_base[kb_key]["count"] += kb_data["count"]
                    master_knowledge_base[kb_key]["prices"].update(kb_data["prices"])

            if "hash_cache" in result:
                for h_key, h_data in result["hash_cache"].items():
                    if h_key not in master_hash_cache:
                        master_hash_cache[h_key] = h_data
                        new_hash_entries[h_key] = h_data

    except KeyboardInterrupt:
        print("\n🛑 Pipeline interrupted by user. Saving partial progress...")

    # =========================================================================
    # ── 3.5 FIND FAILED/MISSING SHOWS ────────────────────────────────────────
    # =========================================================================
    print(f"\n🔄 CHECKING FOR FAILED APIS AND MISSING SHOWS...")

    current_shows_keys = set()
    failed_show_keys = set()

    for row in master_shows_data:
        key = f"{row['t_id']}_{row['time']}"
        if row["status"] == "Available (Map Error)":
            failed_show_keys.add(key)
        else:
            current_shows_keys.add(key)

    for row in master_sold_out_queue:
        current_shows_keys.add(f"{row['t_id']}_{row['time']}")

    missing_show_keys = set()
    missing_theaters = set()
    completely_missing_log = []
    last_snap = []

    if os.path.exists(LAST_SNAPSHOT_FILE):
        try:
            with open(LAST_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                last_snap = json.load(f)

            for row in last_snap:
                if row.get("is_extra"):
                    continue
                if row.get("format") == "Fan Event / Manual":
                    continue

                key = f"{row['t_id']}_{row['time']}"
                if key not in current_shows_keys and key not in failed_show_keys:
                    missing_show_keys.add(key)
                    missing_theaters.add(row["t_id"])

            if missing_show_keys:
                print(
                    f"   => 🔍 Found {len(missing_show_keys)} missing shows "
                    f"across {len(missing_theaters)} theaters from previous snapshot."
                )
                for mk in missing_show_keys:
                    for row in last_snap:
                        if f"{row['t_id']}_{row['time']}" == mk:
                            completely_missing_log.append(
                                f"( {row['state']} ) {row['theater']} - "
                                f"{row['time']} [{row['format']}] - "
                                f"Missing from Fandango API today."
                            )
                            break
        except Exception as e:
            print(
                f"   => ⚠️ Could not load snapshot for missing shows detection: {e}"
            )

    # =========================================================================
    # ── 4. QUEUE PROCESSING (Cross-Referencing) ──────────────────────────────
    # =========================================================================
    if master_sold_out_queue:
        print(
            f"\n🔄 Processing {len(master_sold_out_queue)} Total Sold Out shows from Queue..."
        )

        for item in master_sold_out_queue:
            t_id = item["t_id"]
            fmt = item["format"]

            combined_total = 0
            combined_booked = 0
            combined_gross = 0
            price_str = 0

            master_shows_data.append(
                {
                    "state": item["state"],
                    "t_id": t_id,
                    "theater": item["theater"],
                    "format": fmt,
                    "language": item.get("language", DEFAULT_LANGUAGE),
                    "time": item["time"],
                    "status": "Sold Out (EXTRA)",
                    "format_categories": item.get("format_categories")
                    or classify_report_formats(fmt),
                    "price_str": price_str,
                    "total": combined_total,
                    "booked": combined_booked,
                    "gross": combined_gross,
                }
            )

            master_summary_data[t_id]["shows"] += 1
            master_summary_data[t_id]["total"] += combined_total
            master_summary_data[t_id]["booked"] += combined_booked
            master_summary_data[t_id]["gross"] += combined_gross

    # =========================================================================
    # ── 4.5 MANUAL SHOWS PROCESSING ──────────────────────────────────────────
    # =========================================================================
    manual_notes_text = None
    if MANUAL_SHOWS:
        print(f"\n➕ Processing {len(MANUAL_SHOWS)} Manual/Fan/Extra Shows...")
        detailed_logger.info(f"Processing {len(MANUAL_SHOWS)} Manual Shows...")
        manual_log_lines = []

        for ms in MANUAL_SHOWS:
            try:
                st = str(ms[0]) if len(ms) > 0 and ms[0] is not None else ""

                if st.upper() == "EXTRA":
                    b_gross = (
                        float(ms[3]) if len(ms) > 3 and ms[3] is not None else 0.0
                    )
                    b_seats = int(ms[5]) if len(ms) > 5 and ms[5] is not None else 0

                    master_shows_data.append(
                        {
                            "state": "EXTRA",
                            "t_id": "EXTRA",
                            "theater": "EXTRA",
                            "format": "EXTRA",
                            "language": "EXTRA",
                            "time": "",
                            "status": "EXTRA",
                            "price_str": "",
                            "total": b_seats,
                            "booked": b_seats,
                            "gross": b_gross,
                            "is_extra": True,
                        }
                    )

                    log_msg = f"( EXTRA ) {EXTRA_GROSS_NOTE}."
                    print(
                        f"   => ✅ Added: ( EXTRA ) {EXTRA_GROSS_NOTE}: {b_seats} Tickets | ${b_gross:,.2f}"
                    )
                    manual_log_lines.append(log_msg)
                    continue

                t_id = str(ms[1]).strip().lower() if len(ms) > 1 else ""
                time_str = ms[2] if len(ms) > 2 else ""
                b_gross = float(ms[3]) if len(ms) > 3 and ms[3] is not None else 0.0
                t_gross = float(ms[4]) if len(ms) > 4 and ms[4] is not None else 0.0
                b_seats = int(ms[5]) if len(ms) > 5 and ms[5] is not None else 0
                t_seats = int(ms[6]) if len(ms) > 6 and ms[6] is not None else 0
                occ = float(ms[7]) if len(ms) > 7 and ms[7] is not None else 0.0

                fmt = str(ms[8]) if len(ms) > 8 and ms[8] else "Fan Event / Manual"
                lang = str(ms[9]) if len(ms) > 9 and ms[9] else DEFAULT_LANGUAGE

                t_name = "Unknown Theater"
                if st in master_map:
                    for t in master_map[st]:
                        if (
                            str(t.get("theaterId")).strip().lower() == t_id.lower()
                            or t.get("theaterId") == t_id
                        ):
                            t_name = t.get("theaterName", "Unknown")
                            break

                price_str = f"${(b_gross / b_seats):.2f}" if b_seats > 0 else "$0.00"
                status = (
                    "Sold Out" if b_seats >= t_seats and t_seats > 0 else "Available"
                )

                master_shows_data.append(
                    {
                        "state": st,
                        "t_id": t_id,
                        "theater": t_name,
                        "format": fmt,
                        "language": lang,
                        "time": time_str,
                        "status": status,
                        "format_categories": classify_report_formats(fmt),
                        "price_str": price_str,
                        "total": t_seats,
                        "booked": b_seats,
                        "gross": b_gross,
                    }
                )

                if t_id not in master_summary_data:
                    master_summary_data[t_id] = {
                        "t_id": t_id,
                        "state": st,
                        "name": t_name,
                        "shows": 0,
                        "total": 0,
                        "booked": 0,
                        "gross": 0.0,
                    }

                master_summary_data[t_id]["shows"] += 1
                master_summary_data[t_id]["total"] += t_seats
                master_summary_data[t_id]["booked"] += b_seats
                master_summary_data[t_id]["gross"] += b_gross

                log_msg = f"( {st} ) {t_name} - {time_str} [{fmt} | {lang}] - {b_seats}/{t_seats} Booked | ${b_gross:,.2f}"
                print(f"   => ✅ Added: {log_msg}")
                manual_log_lines.append(log_msg)
            except Exception as e:
                print(f"   => ❌ Error processing manual show {ms}: {e}")

        manual_notes_text = "\n".join(manual_log_lines)

    # =========================================================================
    # ── 4.6 DYNAMIC MULTI-TIER DATA CONSOLIDATION (GENERIC MERGE) ────────────
    # =========================================================================
    if master_shows_data and MANUAL_MERGE_SHOWS_THEATRES:
        print(
            f"\n🔧 Checking post-processing data consolidation rules for {len(MANUAL_MERGE_SHOWS_THEATRES)} target theaters..."
        )

        cleaned_shows_list = []
        global_merger_registry = {}

        target_theaters_lower = [
            str(tid).strip().lower() for tid in MANUAL_MERGE_SHOWS_THEATRES
        ]

        for row in master_shows_data:
            current_t_id = str(row.get("t_id", "")).strip()

            if current_t_id.lower() not in target_theaters_lower:
                cleaned_shows_list.append(row)
                continue

            show_time = row.get("time")
            merge_composite_key = f"{current_t_id}_{show_time}"

            if merge_composite_key not in global_merger_registry:
                global_merger_registry[merge_composite_key] = row
            else:
                baseline_row = global_merger_registry[merge_composite_key]

                baseline_row["total"] += row.get("total", 0)
                baseline_row["booked"] += row.get("booked", 0)
                baseline_row["gross"] += row.get("gross", 0.0)

                current_fmt = str(row.get("format", "")).strip()
                if (
                    current_fmt
                    and current_fmt.lower() not in str(baseline_row["format"]).lower()
                ):
                    baseline_row["format"] = f"{baseline_row['format']} / {current_fmt}"
                baseline_row["format_categories"] = combine_report_format_categories(
                    [baseline_row, row]
                )

                current_price = str(row.get("price_str", "")).strip()
                if (
                    current_price
                    and current_price.lower()
                    not in str(baseline_row["price_str"]).lower()
                ):
                    baseline_row["price_str"] = (
                        f"{baseline_row['price_str']} / {current_price}"
                    )

                if (
                    baseline_row["booked"] >= baseline_row["total"]
                    and baseline_row["total"] > 0
                ):
                    baseline_row["status"] = "Sold Out"
                else:
                    baseline_row["status"] = "Available"

                if current_t_id in master_summary_data:
                    master_summary_data[current_t_id]["shows"] -= 1

                print(
                    f"   => Successfully consolidated duplicate split map for {current_t_id} at {show_time}"
                )

        for merged_row in global_merger_registry.values():
            cleaned_shows_list.append(merged_row)

        master_shows_data = cleaned_shows_list

    # =========================================================================
    # ── 4.7 CHAIN-SPECIFIC SOLD OUT IGNORER ──────────────────────────────────
    # =========================================================================
    ignored_soldout_chain_log = []
    if IGNORE_SOLDOUT_CHAINS:
        print(
            f"\n🚫 Applying Sold Out filter for chains: {', '.join(IGNORE_SOLDOUT_CHAINS)}..."
        )

        for show in master_shows_data:
            if show.get("status") == "Sold Out":
                t_name_lower = show.get("theater", "").lower()

                if any(
                    chain.lower() in t_name_lower for chain in IGNORE_SOLDOUT_CHAINS
                ):
                    log_msg = f"( {show['state']} ) {show['theater']} - {show['time']} [{show['format']}] | Removed: {show['booked']} tickets, ${show['gross']:,.2f}"
                    ignored_soldout_chain_log.append(log_msg)

                    t_id = show["t_id"]

                    if t_id in master_summary_data:
                        master_summary_data[t_id]["total"] -= show["total"]
                        master_summary_data[t_id]["booked"] -= show["booked"]
                        master_summary_data[t_id]["gross"] -= show["gross"]

                    show["total"] = 0
                    show["booked"] = 0
                    show["gross"] = 0.0
                    show["price_str"] = "$0.00"
                    show["status"] = "Sold Out (Ignored)"

    # =========================================================================
    # ── 4.8 CUMULATIVE MODE: RESCUE PASSED SHOWS & PATCH API ERRORS ──────────
    # =========================================================================
    if recent_shows_data:
        print(
            "\n🛡️ Last Run Data Found! Rescuing dropped shows and patching API errors..."
        )
        cumulative_result = reconcile_cumulative_shows(
            master_shows_data,
            master_summary_data,
            recent_shows_data,
            SHOW_DATE,
        )
        master_shows_data = cumulative_result["shows"]
        print(
            "   => ✅ Rescued "
            f"{cumulative_result['rescued']} past shows. "
            f"Dropped {cumulative_result['dropped']} cancelled future shows. "
            f"Patched {cumulative_result['protected']} API errors."
        )

    # =========================================================================
    # ── 5. GENERATE REPORTS & SEND EMAIL ─────────────────────────────────────
    # =========================================================================
    if master_shows_data:
        print(f"\n🚀 Formatting data and building reports...")
        current_time = time.strftime("%Y%m%d_%H%M%S")
        base_name = f"reports/Fandango_USA_{SHOW_DATE}_{current_time}"

        master_hash_cache.update(new_hash_entries)
        save_local_hash_cache(master_hash_cache)
        detailed_logger.info(
            f"Saved complete merged hash cache to local storage: {HASH_CACHE_FILE} "
            f"({len(master_hash_cache)} entries)"
        )

        previous_shows_data = []
        last_updated_str = "First Run"

        if os.path.exists(LAST_SNAPSHOT_FILE):
            try:
                with open(LAST_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                    previous_shows_data = json.load(f)
                mtime = os.path.getmtime(LAST_SNAPSHOT_FILE)
                last_updated_str = datetime.fromtimestamp(mtime).strftime(
                    "%d %b %Y, %I:%M %p"
                )
                print(
                    f"📈 Loaded previous snapshot from {last_updated_str} for momentum tracking."
                )
            except Exception as e:
                print(f"⚠️ Failed to load previous snapshot: {e}")

        try:
            export_master_excel(
                master_shows_data,
                base_name,
                previous_shows_data=previous_shows_data,
                last_updated_str=last_updated_str,
            )
        except Exception as e:
            print(f"⚠️ Failed to generate Excel report: {e}")

        img_path = f"{base_name}.png"
        try:
            generate_fandango_image_report(
                master_shows_data,
                img_path,
                movie_name=MOVIE_TITLE,
                show_date=SHOW_DATE,
                previous_shows_data=previous_shows_data,
                last_updated_str=last_updated_str,
                country_name="USA",
            )
        except Exception as e:
            print(f"⚠️ Failed to generate Image report: {e}")

        html_path = f"{base_name}.html"
        try:
            generate_fandango_html_report(
                master_shows_data,
                html_path,
                movie_name=MOVIE_TITLE,
                show_date=SHOW_DATE,
                previous_shows_data=previous_shows_data,
                last_updated_str=last_updated_str,
                country_name="USA",
            )
        except Exception as e:
            print(f"⚠️ Failed to generate HTML report: {e}")

        if master_hash_healed_log:
            print("\n=====================================================================")
            print("🟢 HASH CACHE HEALED SHOWS (100% Accurate Recovery)")
            print("The following Sold Out shows crashed the Fandango API, but their exact")
            print("physical seating capacities and prices were instantly restored using Firebase memory.")
            print("=====================================================================")
            for log in master_hash_healed_log:
                print(log)
            print("=====================================================================\n")

        if master_sold_out_queue:
            print("\n=====================================================================")
            print("⚠️ SOLD-OUT SHOWS WITHOUT DATA — MANUAL REVIEW REQUIRED")
            print("These shows are sold out, but seat-map data was unavailable.")
            print("Check their actual gross/tickets, then add verified values to MANUAL_SHOWS.")
            print("They were not estimated or included in the report totals.")
            print("=====================================================================")
            for item in master_sold_out_queue:
                print(
                    f"( {item['state']} ) {item['theater']} - {item['time']} "
                    f"[{item['format']} | {item.get('language', DEFAULT_LANGUAGE)}]"
                )
                if item.get("seat_map_urls"):
                    print(f"   Seat map URL(s): {item['seat_map_urls']}")
            print("=====================================================================\n")

        if ignored_soldout_chain_log:
            print("\n=====================================================================")
            print("🚫 IGNORED SOLD OUT SHOWS (Specific Chains)")
            print(f"The following Sold Out shows belonged to: {', '.join(IGNORE_SOLDOUT_CHAINS)}")
            print("Their Gross and Tickets have been reverted to 0.")
            print("=====================================================================")
            for log in ignored_soldout_chain_log:
                print(log)
            print("=====================================================================\n")

        if master_ignored_shows_log:
            print("\n=====================================================================")
            print("⚠️ SHOWS WITH MAP ERRORS (0 Capacity Logged)")
            print("The following shows are available but we were not able to fetch seats.")
            print("They have been included in the reports to count the show, but with 0 seats/gross.")
            print("=====================================================================")
            for log in master_ignored_shows_log:
                print(log)
            print("=====================================================================\n")

        if master_rate_limit_shows:
            print("\n=====================================================================")
            print(
                f"⚠️ RATE LIMIT WARNING: observed {len(master_rate_limit_shows)} "
                "HTTP 403/429 responses."
            )
            print("=====================================================================\n")

        momentum_msg = ""
        if previous_shows_data:
            prev_gross = sum(r.get("gross", 0) for r in previous_shows_data)
            prev_tickets = sum(r.get("booked", 0) for r in previous_shows_data)
            curr_gross = sum(r.get("gross", 0) for r in master_shows_data)
            curr_tickets = sum(r.get("booked", 0) for r in master_shows_data)

            diff_gross = curr_gross - prev_gross
            diff_tickets = curr_tickets - prev_tickets

            sign_t = "+" if diff_tickets >= 0 else ""
            sign_g = "+" if diff_gross >= 0 else ""

            momentum_msg = f"🚀 MOMENTUM UPDATE:\n{sign_t}{diff_tickets:,} Tickets and {sign_g}${diff_gross:,.2f} Gross added since {last_updated_str} IST\n\n"

        # --- FIREBASE V2 PUBLISH ---
        if FIREBASE_READY:
            if new_hash_entries:
                print(
                    f"💾 Saving {len(new_hash_entries)} newly learned Hash Blueprints to Firebase..."
                )
                db.reference(
                    f"{firebase_movie_root()}/scraper_state/hash_capacity_cache"
                ).update(new_hash_entries)
                print("✅ Hash Cache updated successfully.")

            run_timestamp = datetime.now(timezone.utc)
            analytics_payload = build_analytics_payload(
                master_shows_data,
                market_id="usa",
                movie_id=MOVIE_SLUG,
                show_date=SHOW_DATE,
                run_id=run_timestamp.strftime("%Y%m%dT%H%M%S%fZ"),
                previous_rows=recent_shows_data,
                daily_baseline_rows=daily_baseline_rows,
                previous_run_id=active_run_id,
                previous_run_observed_at=recent_shows_timestamp,
                daily_snapshot_id=daily_snapshot_id,
                daily_snapshot_observed_at=daily_snapshot_observed_at,
                currency="USD",
            )
            publish_analytics_payload(
                db,
                "usa",
                MOVIE_SLUG,
                SHOW_DATE,
                analytics_payload,
                update_daily_baseline=OVERRIDE_FIREBASE_DAILY_SNAPSHOT,
                update_advance_snapshot=(
                    datetime.strptime(SHOW_DATE, "%Y-%m-%d").date()
                    > datetime.now(pytz.timezone("America/Chicago")).date()
                    or FORCE_UPDATE_ADVANCE
                ),
                as_of_date=datetime.now(pytz.timezone("America/Chicago"))
                .date()
                .isoformat(),
            )
            print("✅ V2 analytics summary and detail pages published.")

            try:
                movie_metadata = {
                    key: value for key, value in MOVIE_METADATA.items() if value
                }
                db.reference(f"markets/usa/movies/{MOVIE_SLUG}/meta").update(
                    {
                        "title": MOVIE_TITLE,
                        "global_slug": GLOBAL_MOVIE_SLUG.strip(),
                        **movie_metadata,
                    }
                )
                db.reference(f"markets/usa/movie_index/{MOVIE_SLUG}").update(
                    {
                        "movie_id": MOVIE_SLUG,
                        "name": MOVIE_TITLE,
                        "global_slug": GLOBAL_MOVIE_SLUG.strip(),
                        **movie_metadata,
                    }
                )

                from build_portal_views import DEFAULT_INR_TO_USD, run_portal_views

                exchange_rate_setting = os.environ.get("INR_TO_USD", "").strip()
                exchange_rate = (
                    float(exchange_rate_setting)
                    if exchange_rate_setting
                    else DEFAULT_INR_TO_USD
                )
                run_portal_views(
                    market_ids=("usa",),
                    inr_to_usd=exchange_rate,
                    db_instance=db,
                )
                print("✅ USA and global portal views rebuilt.")
            except Exception:
                logging.exception("Automatic USA/global portal refresh failed.")
                raise

        if momentum_msg:
            manual_notes_text = (
                (manual_notes_text or "") + "\n\n" + momentum_msg
            ).strip()

        pipeline_elapsed = time.time() - PIPELINE_START_TIME
        log_rate_limit_summary(detailed_logger, master_rate_limit_shows)
        db.reference("fandango_cron_currentindex").set(FANDANGO_QUEUE_NEXT_INDEX)
        print(f"✅ Queue cursor advanced to {FANDANGO_QUEUE_NEXT_INDEX}.")
        mins, secs = divmod(pipeline_elapsed, 60)
        print(
            f"🎉 Pipeline completely finished in {int(mins)} minutes and {int(secs)} seconds!"
        )
        detailed_logger.info("🎉 PIPELINE FINISHED SUCCESSFULLY.")

        if OVERRIDE_LOCAL_SNAPSHOT:
            try:
                with open(LAST_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
                    json.dump(master_shows_data, f)
                print(
                    f"💾 Saved current data to {LAST_SNAPSHOT_FILE} for future momentum tracking."
                )
            except Exception as e:
                print(f"⚠️ Failed to save snapshot: {e}")
        else:
            print(
                f"⏭️ Skipping snapshot save (OVERRIDE_LOCAL_SNAPSHOT is set to False)."
            )

    else:
        print("❌ No successful showtime seat maps were fetched across any state.")
        log_rate_limit_summary(detailed_logger, master_rate_limit_shows)
        detailed_logger.info("❌ PIPELINE FAILED OR NO MAPS FETCHED.")
        raise RuntimeError("Queue item was not advanced because no showtime seat maps were fetched.")