"""
PHASE 1 — STEP 1: Geocoding
============================
Takes city + pincode + country and returns (lat, lng).
Uses Nominatim (OpenStreetMap) — free, no API key needed.

Usage:
    python phase1_step1_geocode.py
"""

import requests
import time

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

HEADERS = {
    #Personal email ID is used here in case of any issue
    "User-Agent": "CarDealerFinder/1.0 (priyanshu12jain@gmail.com)"
}


def geocode(city: str, pincode: str, country: str) -> dict:
    """
    Convert city + pincode + country to lat/lng.

    Returns:
        {
            "success": True,
            "lat": 3.1234,
            "lng": 101.5678,
            "display_name": "Full address string from Nominatim",
            "query_used": "which query string worked"
        }
        or
        {
            "success": False,
            "error": "reason"
        }
    """

    # Strategy: try 3 queries in order, most specific to least specific
    queries = [
        f"{pincode}, {city}, {country}",   # Most specific: pincode + city
        f"{pincode}, {country}",            # Fallback 1: pincode only
        f"{city}, {country}",              # Fallback 2: city only
    ]

    for query in queries:
        print(f"  Trying query: '{query}'")

        params = {
            "q": query,
            "format": "json",
            "limit": 1,           # We only need the top result
            "addressdetails": 1,  # Get structured address back
        }

        try:
            response = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=10)
            response.raise_for_status()
            results = response.json()

            if results:
                top = results[0]
                lat = float(top["lat"])
                lng = float(top["lon"])
                display_name = top.get("display_name", "")

                print(f"  [OK] Found: {display_name[:80]}...")
                return {
                    "success": True,
                    "lat": lat,
                    "lng": lng,
                    "display_name": display_name,
                    "query_used": query
                }
            else:
                print(f"  [FAIL] No results for this query, trying next...")

            # Nominatim rate limit: max 1 request/second — be respectful
            time.sleep(1)

        except requests.exceptions.Timeout:
            print(f"  [FAIL] Request timed out for query: {query}")
        except requests.exceptions.RequestException as e:
            print(f"  [FAIL] Request error: {e}")

    # All queries failed
    return {
        "success": False,
        "error": f"Could not geocode: city='{city}', pincode='{pincode}', country='{country}'"
    }


# ─── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    print("=" * 60)
    print("GEOCODER — City + Pincode → Lat/Lng")
    print("=" * 60)

    city    = input("\nEnter city name   : ").strip()
    pincode = input("Enter pincode     : ").strip()
    country = input("Enter country     : ").strip()

    print()
    result = geocode(city, pincode, country)

    if result["success"]:
        print(f"\n[Result]:")
        print(f"  Lat         : {result['lat']}")
        print(f"  Lng         : {result['lng']}")
        print(f"  Full name   : {result['display_name']}")
        print(f"  Query used  : {result['query_used']}")
    else:
        print(f"\n[Failed]: {result['error']}")