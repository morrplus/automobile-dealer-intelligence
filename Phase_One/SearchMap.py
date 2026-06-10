"""
PHASE 1 — STEP 2: Search Dealers via SerpAPI
=============================================
- Takes city + pincode + country as input
- Calls converting.py to get lat/lng
- Feeds coordinates into SerpAPI Google Maps engine
- Returns a clean list of car dealers

Folder structure:
    geocoding/
        converting.py   ← geocoding (already built)
        search_dealers.py  ← this file

Usage:
    python search_dealers.py
"""

import requests
import json
import time
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
import os
from converting import geocode

load_dotenv()
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

# ─── CONFIG ────────────────────────────────────────────────────────────────────

SERPAPI_URL   = "https://serpapi.com/search"
SEARCH_QUERY  = "Used car dealer"

# ─── SEARCH FUNCTION ───────────────────────────────────────────────────────────

def search_dealers(lat: float, lng: float, query: str = SEARCH_QUERY) -> dict:
    """
    Call SerpAPI Google Maps with coordinates and return raw results.

    Returns:
        {
            "success": True,
            "results": [ ...list of dealer dicts... ],
            "raw": { ...full API response... }
        }
        or
        {
            "success": False,
            "error": "reason"
        }
    """

    params = {
        "engine":   "google_maps",         # Use Google Maps engine
        "q":        query,                 # Search query
        "ll":       f"@{lat},{lng},14z",   # Coordinates + zoom level
        "type":     "search",              # Search mode (not place lookup)
        "api_key":  SERPAPI_KEY,
    }

    print(f"  Searching: '{query}' near ({lat}, {lng})...")

    try:
        response = requests.get(SERPAPI_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Check if SerpAPI returned an error
        if "error" in data:
            return {
                "success": False,
                "error": data["error"]
            }

        raw_results = data.get("local_results", [])
        print(f"  ✓ Got {len(raw_results)} results from SerpAPI")

        return {
            "success": True,
            "results": raw_results,
            "raw": data
        }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "SerpAPI request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}


# ─── PARSE FUNCTION ────────────────────────────────────────────────────────────

def parse_dealers(raw_results: list) -> list:
    """
    Extract only the fields we care about from raw SerpAPI results.
    Skips results that don't look like real dealers (aggregators, etc.)

    Returns a clean list of dicts:
        [
            {
                "name": "...",
                "address": "...",
                "phone": "...",       # may be None
                "website": "...",     # may be None
                "rating": 4.2,        # may be None
                "place_id": "...",    # Google's unique ID — keep this!
                "type": "..."
            },
            ...
        ]
    """

    # Domains to skip — these are aggregators, not actual dealers
    SKIP_DOMAINS = [
        "carlist.my", "mudah.my", "olx.com", "carbay.my",
        "carsome.my", "cars.com", "cardekho.com", "carwale.com"
    ]

    cleaned = []

    for item in raw_results:
        name    = item.get("title", "").strip()
        address = item.get("address", "").strip()
        phone   = item.get("phone")
        website = item.get("website")
        rating  = item.get("rating")
        place_id = item.get("place_id")
        biz_type = item.get("type", "")

        # Skip if no name (shouldn't happen but just in case)
        if not name:
            continue

        # Skip aggregator websites
        if website:
            if any(domain in website for domain in SKIP_DOMAINS):
                print(f"  ⚠ Skipping aggregator: {name} ({website})")
                continue

        cleaned.append({
            "name":     name,
            "address":  address,
            "phone":    phone,
            "website":  website,
            "rating":   rating,
            "place_id": place_id,
            "type":     biz_type
        })

    return cleaned


# ─── DISPLAY FUNCTION ──────────────────────────────────────────────────────────

def display_dealers(dealers: list):
    """Print dealers in a readable format."""

    if not dealers:
        print("\n  No dealers found.")
        return

    print(f"\n{'=' * 60}")
    print(f"  FOUND {len(dealers)} DEALERS")
    print(f"{'=' * 60}")

    for i, d in enumerate(dealers, 1):
        print(f"\n  [{i}] {d['name']}")
        print(f"      Address : {d['address'] or 'N/A'}")
        print(f"      Phone   : {d['phone'] or 'N/A'}")
        print(f"      Website : {d['website'] or 'N/A'}")
        print(f"      Rating  : {d['rating'] or 'N/A'}")
        print(f"      Type    : {d['type'] or 'N/A'}")
        print(f"      PlaceID : {d['place_id'] or 'N/A'}")


# ─── SAVE FUNCTION ─────────────────────────────────────────────────────────────

def save_to_json(dealers: list, city: str, pincode: str):
    """Save results to a JSON file named after the city+pincode."""

    filename = f"dealers_{city.replace(' ', '_')}_{pincode}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(dealers, f, indent=2, ensure_ascii=False)
    print(f"\n  ✓ Saved to {filename}")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    print("=" * 60)
    print("DEALER SEARCH — Powered by SerpAPI + Google Maps")
    print("=" * 60)

    # Step 1: Get location from user
    city    = input("\nEnter city name : ").strip()
    pincode = input("Enter pincode   : ").strip()
    country = "Malaysia"

    # Step 2: Geocode → get lat/lng (calls converting.py)
    print(f"\nGeocoding '{city}, {pincode}, {country}'...")
    geo = geocode(city, pincode, country)

    if not geo["success"]:
        print(f"\n✗ Geocoding failed: {geo['error']}")
        exit(1)

    print(f"✓ Coordinates: {geo['lat']}, {geo['lng']}")

    # Step 3: Search SerpAPI with coordinates
    print()
    search = search_dealers(geo["lat"], geo["lng"])

    if not search["success"]:
        print(f"\n✗ Search failed: {search['error']}")
        exit(1)

    # Step 4: Parse and clean results
    dealers = parse_dealers(search["results"])

    # Step 5: Display
    display_dealers(dealers)

    # Step 6: Save to JSON
    if dealers:
        save = input("\nSave results to JSON? (y/n) : ").strip().lower()
        if save == "y":
            save_to_json(dealers, city, pincode)