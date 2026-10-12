"""Import a Fandango hash-capacity export into one movie/date cache."""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path

DEFAULT_MOVIE_SLUG = "jailer-2"
DEFAULT_SHOW_DATE = "2026-10-14"
DEFAULT_EXPORT_PATH = Path(__file__).resolve().with_name(
    "fandango-web-default-rtdb-hash_capacity_cache-export.json"
)


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


def load_hash_cache_export(export_path: Path) -> dict:
    """Load and validate an exported {hash_id: cache_entry} JSON object."""
    with export_path.open("r", encoding="utf-8") as export_file:
        exported_entries = json.load(export_file)
    if not isinstance(exported_entries, dict) or not exported_entries:
        raise ValueError("The cache export must be a non-empty JSON object keyed by hash ID.")

    cache_entries = {}
    for hash_id, entry in exported_entries.items():
        hash_id = validate_database_key(hash_id, "hash-id")
        if not isinstance(entry, dict):
            raise ValueError(f"Cache entry {hash_id} must be a JSON object.")

        capacity = entry.get("capacity")
        show_format = entry.get("format")
        max_gross = entry.get("max_gross")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValueError(f"Cache entry {hash_id} capacity must be a whole number >= 0.")
        if not isinstance(show_format, str) or not show_format.strip():
            raise ValueError(f"Cache entry {hash_id} format must not be empty.")
        if isinstance(max_gross, bool) or not isinstance(max_gross, (int, float)):
            raise ValueError(f"Cache entry {hash_id} max_gross must be a number.")
        if not math.isfinite(max_gross) or max_gross < 0:
            raise ValueError(f"Cache entry {hash_id} max_gross must be finite and >= 0.")

        cache_entries[hash_id] = {
            "capacity": capacity,
            "format": show_format.strip(),
            "max_gross": float(max_gross),
        }
    return cache_entries


def add_hash_cache_entries(
    movie_slug: str,
    show_date: str,
    cache_entries: dict,
) -> tuple[str, int]:
    movie_slug = validate_database_key(movie_slug, "movie-slug")
    show_date = validate_show_date(show_date)
    if not cache_entries:
        raise ValueError("The cache export contains no entries to import.")

    firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
    firebase_db_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not firebase_creds_json or not firebase_db_url:
        raise RuntimeError("FIREBASE_CREDENTIALS and FIREBASE_DATABASE_URL must be set in the environment or .env file.")

    import firebase_admin
    from firebase_admin import credentials, db

    credentials_data = json.loads(firebase_creds_json)
    if not isinstance(credentials_data, dict):
        raise ValueError("FIREBASE_CREDENTIALS must contain a JSON object.")

    if not firebase_admin._apps:
        credential = credentials.Certificate(credentials_data)
        firebase_admin.initialize_app(credential, {"databaseURL": firebase_db_url})

    cache_path = (
        f"markets/usa/movies/{movie_slug}/dates/{show_date}"
        "/scraper_state/hash_capacity_cache"
    )
    cache_ref = db.reference(cache_path)
    cache_ref.update(cache_entries)
    return cache_path, len(cache_entries)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append missing hash-capacity entries from a Fandango JSON export."
    )
    parser.add_argument("--movie-slug", default=DEFAULT_MOVIE_SLUG)
    parser.add_argument("--show-date", default=DEFAULT_SHOW_DATE)
    parser.add_argument("--input", type=Path, default=DEFAULT_EXPORT_PATH)
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
    cache_entries = load_hash_cache_export(args.input)
    cache_path, imported_count = add_hash_cache_entries(
        movie_slug=args.movie_slug,
        show_date=args.show_date,
        cache_entries=cache_entries,
    )
    print(
        f"Imported {imported_count} hash entr{'y' if imported_count == 1 else 'ies'} "
        f"at: {cache_path}"
    )


if __name__ == "__main__":
    main()
