"""Add or update multiple Fandango tier entries in Firebase's hash capacity cache."""

from datetime import datetime
import json
import math
import os
from pathlib import Path

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, db

# Set these values before running the script.
MOVIE_SLUG = ""
SHOW_DATE = ""
HASH_CACHE_ENTRIES = [
    # Add one dictionary per show/tier:
    # {"hash_id": "v2-...", "capacity": 198, "format": "Cinemark XD", "max_gross": 6326.10},
]


def validate_database_key(value: str, name: str) -> str:
    """Ensure a value is safe to use as one Firebase Realtime Database path key."""
    if not value or any(char in value for char in ".#$[]/") or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must be a non-empty Firebase key without . # $ [ ] / or control characters.")
    return value


def validate_show_date(value: str) -> str:
    try:
        parsed_date = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("show-date must be a valid date in YYYY-MM-DD format.") from error
    if parsed_date.strftime("%Y-%m-%d") != value:
        raise ValueError("show-date must be in YYYY-MM-DD format.")
    return value


def add_hash_cache_entries(
    movie_slug: str,
    show_date: str,
) -> tuple[str, int]:
    movie_slug = validate_database_key(movie_slug, "movie-slug")
    show_date = validate_show_date(show_date)

    if not HASH_CACHE_ENTRIES:
        raise ValueError("Add at least one entry to HASH_CACHE_ENTRIES.")

    cache_entries = {}
    for index, entry in enumerate(HASH_CACHE_ENTRIES, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"HASH_CACHE_ENTRIES item {index} must be a dictionary.")

        hash_id = validate_database_key(entry.get("hash_id", ""), f"item {index} hash-id")
        capacity = entry.get("capacity")
        show_format = entry.get("format")
        max_gross = entry.get("max_gross")

        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValueError(f"Item {index} capacity must be a whole number that is zero or greater.")
        if not isinstance(show_format, str) or not show_format.strip():
            raise ValueError(f"Item {index} format must not be empty.")
        if isinstance(max_gross, bool) or not isinstance(max_gross, (int, float)):
            raise ValueError(f"Item {index} max_gross must be a number.")
        if not math.isfinite(max_gross) or max_gross < 0:
            raise ValueError(f"Item {index} max_gross must be finite and zero or greater.")
        if hash_id in cache_entries:
            raise ValueError(f"Duplicate hash_id in HASH_CACHE_ENTRIES: {hash_id}")

        cache_entries[hash_id] = {
            "capacity": capacity,
            "format": show_format.strip(),
            "max_gross": float(max_gross),
        }

    firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
    firebase_db_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not firebase_creds_json or not firebase_db_url:
        raise RuntimeError("FIREBASE_CREDENTIALS and FIREBASE_DATABASE_URL must be set in the environment or .env file.")

    credentials_data = json.loads(firebase_creds_json)
    if not isinstance(credentials_data, dict):
        raise ValueError("FIREBASE_CREDENTIALS must contain a JSON object.")

    if not firebase_admin._apps:
        credential = credentials.Certificate(credentials_data)
        firebase_admin.initialize_app(credential, {"databaseURL": firebase_db_url})

    cache_path = f"markets/usa/movies/{movie_slug}/{show_date}/hash_capacity_cache"
    db.reference(cache_path).update(cache_entries)
    return cache_path, len(cache_entries)


def main() -> None:
    if not MOVIE_SLUG or not SHOW_DATE:
        raise ValueError("Set MOVIE_SLUG and SHOW_DATE at the top of the script.")

    load_dotenv(Path(__file__).resolve().parent / ".env")
    cache_path, entry_count = add_hash_cache_entries(
        movie_slug=MOVIE_SLUG,
        show_date=SHOW_DATE,
    )
    print(f"Updated {entry_count} hash cache entr{'y' if entry_count == 1 else 'ies'} at: {cache_path}")


if __name__ == "__main__":
    main()
