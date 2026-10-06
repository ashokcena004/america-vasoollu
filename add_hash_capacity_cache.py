"""Add or update one Fandango tier entry in Firebase's hash capacity cache."""

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
HASH_ID = ""
CAPACITY = None
SHOW_FORMAT = ""
MAX_GROSS = None


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


def add_hash_cache_entry(
    movie_slug: str,
    show_date: str,
    hash_id: str,
    capacity: int,
    show_format: str,
    max_gross: float,
) -> str:
    movie_slug = validate_database_key(movie_slug, "movie-slug")
    show_date = validate_show_date(show_date)
    hash_id = validate_database_key(hash_id, "hash-id")

    if capacity < 0:
        raise ValueError("capacity must be zero or greater.")
    if not show_format.strip():
        raise ValueError("format must not be empty.")
    if not math.isfinite(max_gross) or max_gross < 0:
        raise ValueError("max-gross must be a finite number that is zero or greater.")

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
    cache_entry = {
        "capacity": capacity,
        "format": show_format.strip(),
        "max_gross": max_gross,
    }
    db.reference(cache_path).child(hash_id).update(cache_entry)
    return f"{cache_path}/{hash_id}"


def main() -> None:
    if not MOVIE_SLUG or not SHOW_DATE or not HASH_ID or not SHOW_FORMAT:
        raise ValueError("Set MOVIE_SLUG, SHOW_DATE, HASH_ID, and SHOW_FORMAT at the top of the script.")
    if isinstance(CAPACITY, bool) or not isinstance(CAPACITY, int):
        raise ValueError("Set CAPACITY to a whole number at the top of the script.")
    if isinstance(MAX_GROSS, bool) or not isinstance(MAX_GROSS, (int, float)):
        raise ValueError("Set MAX_GROSS to a number at the top of the script.")

    load_dotenv(Path(__file__).resolve().parent / ".env")
    cache_path = add_hash_cache_entry(
        movie_slug=MOVIE_SLUG,
        show_date=SHOW_DATE,
        hash_id=HASH_ID,
        capacity=CAPACITY,
        show_format=SHOW_FORMAT,
        max_gross=float(MAX_GROSS),
    )
    print(f"Hash capacity cache updated at: {cache_path}")


if __name__ == "__main__":
    main()
