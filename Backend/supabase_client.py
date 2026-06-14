"""
supabase_client.py — Supabase integration for FindIt
Handles storing and retrieving enriched dealer data.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

_client = None


def get_client():
    """Return a cached Supabase client (lazy init)."""
    global _client
    if _client is not None:
        return _client

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set in .env before using Supabase."
        )

    try:
        from supabase import create_client
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return _client
    except ImportError:
        raise RuntimeError("supabase package not installed. Run: pip install supabase")


def is_configured() -> bool:
    """Check if Supabase credentials are present in .env."""
    return bool(SUPABASE_URL and SUPABASE_KEY and
                SUPABASE_URL != "https://your-project-id.supabase.co")


# ─── TABLE SCHEMA (run once in Supabase SQL editor) ────────────────────────────
#
# CREATE TABLE IF NOT EXISTS dealers (
#     id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
#     place_id        TEXT UNIQUE,
#     name            TEXT,
#     city            TEXT,
#     pincode         TEXT,
#     dealer_type     TEXT,
#     address         TEXT,
#     phone           TEXT,
#     website         TEXT,
#     google_maps_url TEXT,
#     email           TEXT,
#     linkedin_url    TEXT,
#     facebook_url    TEXT,
#     instagram_url   TEXT,
#     score           INTEGER,
#     raw_data        JSONB,
#     created_at      TIMESTAMPTZ DEFAULT NOW(),
#     updated_at      TIMESTAMPTZ DEFAULT NOW()
# );
#
# ─────────────────────────────────────────────────────────────────────────────


def upsert_dealers(dealers: list[dict], city: str, pincode: str, dealer_type: str) -> dict:
    """
    Upsert a list of enriched dealer dicts into the 'dealers' table.
    Uses place_id as the unique key (conflict resolution = update).
    Returns {'inserted': N, 'errors': [...]}
    """
    if not is_configured():
        return {"inserted": 0, "errors": ["Supabase not configured"]}

    client = get_client()
    inserted = 0
    errors = []

    for dealer in dealers:
        try:
            scoring = dealer.get("scoring") or {}
            contacts = dealer.get("contacts") or {}
            socials  = dealer.get("social_media") or {}

            row = {
                "place_id":        dealer.get("place_id") or dealer.get("name", ""),
                "name":            dealer.get("name", ""),
                "city":            city,
                "pincode":         pincode,
                "dealer_type":     dealer_type,
                "address":         dealer.get("address", ""),
                "phone":           dealer.get("phone", ""),
                "website":         dealer.get("website", ""),
                "google_maps_url": dealer.get("google_maps_url", ""),
                "email":           contacts.get("email", ""),
                "linkedin_url":    contacts.get("linkedin", ""),
                "facebook_url":    socials.get("facebook", ""),
                "instagram_url":   socials.get("instagram", ""),
                "score":           scoring.get("score", 0),
                "raw_data":        dealer,   # full JSON stored for reference
            }

            client.table("dealers").upsert(row, on_conflict="place_id").execute()
            inserted += 1

        except Exception as e:
            errors.append({"dealer": dealer.get("name", "?"), "error": str(e)})

    return {"inserted": inserted, "errors": errors}


def fetch_dealers(city: str, pincode: str, dealer_type: str = "both") -> list[dict]:
    """
    Fetch enriched dealers for a given city/pincode from Supabase.
    Returns list of raw_data dicts (the full enriched dealer objects).
    """
    if not is_configured():
        return []

    client = get_client()

    query = (
        client.table("dealers")
        .select("raw_data, score")
        .eq("city", city)
        .eq("pincode", pincode)
        .order("score", desc=True)
    )

    if dealer_type != "both":
        query = query.eq("dealer_type", dealer_type)

    result = query.execute()
    rows = result.data or []

    dealers = []
    for row in rows:
        raw = row.get("raw_data")
        if raw:
            if isinstance(raw, str):
                raw = json.loads(raw)
            dealers.append(raw)

    return dealers


def delete_dealers(city: str, pincode: str) -> int:
    """Delete all dealer records for a city/pincode (for cache reset). Returns rows deleted."""
    if not is_configured():
        return 0

    client = get_client()
    result = client.table("dealers").delete().eq("city", city).eq("pincode", pincode).execute()
    return len(result.data or [])
