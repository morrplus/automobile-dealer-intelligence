"""
supabase_client.py — Supabase integration for FindIt
Handles storing and retrieving enriched dealer data.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

import re

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

    # Lazy import to avoid circular dependency
    from jinaweb_logic import extract_city

    for dealer in dealers:
        try:
            scoring = dealer.get("scoring") or {}
            links   = dealer.get("links") or {}

            # Extract URLs from the nested links structure
            website_url   = (links.get("website") or {}).get("url") or dealer.get("website", "")
            facebook_url  = (links.get("facebook") or {}).get("url") or ""
            instagram_url = (links.get("instagram") or {}).get("url") or ""

            # emails is a list — take first one for the flat column, support string fallback and singular key fallback
            emails_list = dealer.get("emails")
            if isinstance(emails_list, str):
                emails_list = [emails_list]
            elif not emails_list:
                # Check singular email key (some raw_data or CLI formats might use it)
                email_val = dealer.get("email")
                if email_val:
                    emails_list = [email_val] if isinstance(email_val, str) else email_val

            email_str = emails_list[0] if emails_list else ""

            linkedin_url  = dealer.get("linkedin") or ""

            # Resolve actual city from address, fallback to normalized query city
            addr = dealer.get("address", "")
            resolved_city = extract_city(addr)
            if not resolved_city or resolved_city == "Malaysia":
                resolved_city = city.strip().title()
            else:
                resolved_city = resolved_city.strip().title()

            # Try to extract actual postcode from address (5 digits)
            actual_pincode = None
            if addr:
                pincode_matches = re.findall(r"\b\d{5}\b", addr)
                if pincode_matches:
                    actual_pincode = pincode_matches[0]
            if not actual_pincode:
                actual_pincode = pincode

            # Auto-generate maps URL if empty
            google_maps_url = dealer.get("google_maps_url") or ""
            if not google_maps_url and dealer.get("place_id"):
                google_maps_url = f"https://www.google.com/maps/place/?q=place_id:{dealer.get('place_id')}"

            row = {
                "place_id":        dealer.get("place_id") or dealer.get("name", ""),
                "name":            dealer.get("name", ""),
                "city":            resolved_city,
                "pincode":         actual_pincode,
                "dealer_type":     dealer_type,
                "address":         addr,
                "phone":           dealer.get("phone") or "",
                "website":         website_url,
                "google_maps_url": google_maps_url,
                "email":           email_str,
                "linkedin_url":    linkedin_url,
                "facebook_url":    facebook_url,
                "instagram_url":   instagram_url,
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
