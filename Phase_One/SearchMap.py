"""
PHASE 1 — STEP 2: Search Dealers via SerpAPI
=============================================
- Takes city + pincode + country as input
- Asks dealer type (used / new / both) and how many profiles needed
- Calls converting.py to get lat/lng
- Feeds coordinates into SerpAPI Google Maps engine
- Expands radius automatically if target not met
- Returns a clean list of car dealers

Usage:
    python SearchMap.py
"""

import requests
import json
import time
from dotenv import load_dotenv
import os
from pathlib import Path
from converting import geocode

load_dotenv(Path(__file__).parent.parent / ".env")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

# ─── CONFIG ────────────────────────────────────────────────────────────────────

SERPAPI_URL = "https://serpapi.com/search"

# Zoom levels — lower = wider radius
# 14z ≈ city block level
# 13z ≈ neighbourhood/suburb level
# 12z ≈ city level
# 11z ≈ state level (max we go before declaring full state scanned)
ZOOM_LEVELS = ["14z", "13z", "12z", "11z"]
ZOOM_LABELS = {
    "14z": "immediate area",
    "13z": "wider neighbourhood",
    "12z": "full city",
    "11z": "entire state",
}

# Aggregator domains to skip
SKIP_DOMAINS = [
    "carlist.my", "mudah.my", "olx.com", "carbay.my",
    "carsome.my", "cars.com", "cardekho.com", "carwale.com"
]

# ─── DEALER TYPE → QUERY ───────────────────────────────────────────────────────

def get_search_query(dealer_type: str) -> str:
    """Returns the SerpAPI search query based on dealer type chosen by user."""
    if dealer_type == "used":
        return "Used car dealer"
    elif dealer_type == "new":
        return "New car dealer"
    else:  # both
        return "Car dealer"

# ─── SEARCH FUNCTION ───────────────────────────────────────────────────────────

def search_dealers(lat: float, lng: float, query: str, zoom: str = "14z") -> dict:
    """
    Call SerpAPI Google Maps with coordinates and zoom level.
    Returns raw results dict.
    """
    params = {
        "engine":  "google_maps",
        "q":       query,
        "ll":      f"@{lat},{lng},{zoom}",
        "type":    "search",
        "api_key": SERPAPI_KEY,
    }

    print(f"  Searching: '{query}' near ({lat}, {lng}) at zoom {zoom} [{ZOOM_LABELS[zoom]}]...")

    try:
        response = requests.get(SERPAPI_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            return {"success": False, "error": data["error"]}

        raw_results = data.get("local_results", [])
        print(f"  ✓ Got {len(raw_results)} results from SerpAPI")

        return {"success": True, "results": raw_results}

    except requests.exceptions.Timeout:
        return {"success": False, "error": "SerpAPI request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}


# ─── PARSE FUNCTION ────────────────────────────────────────────────────────────

def parse_dealers(raw_results: list) -> list:
    """
    Clean and filter raw SerpAPI results.
    Skips aggregator websites and entries with no name.
    Deduplicates by place_id.
    """
    cleaned = []
    seen_ids = set()

    for item in raw_results:
        name     = item.get("title", "").strip()
        address  = item.get("address", "").strip()
        phone    = item.get("phone")
        website  = item.get("website")
        rating   = item.get("rating")
        place_id = item.get("place_id")
        biz_type = item.get("type", "")

        if not name:
            continue

        # Deduplicate
        if place_id and place_id in seen_ids:
            continue
        if place_id:
            seen_ids.add(place_id)

        # Skip aggregator websites
        if website and any(domain in website for domain in SKIP_DOMAINS):
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


# ─── RADIUS EXPANSION LOOP ─────────────────────────────────────────────────────

def collect_dealers(lat: float, lng: float, query: str, target: int) -> list:
    """
    Collects dealers, expanding radius if target not met.

    Zoom levels tried in order:
        14z → 13z → 12z → 11z (entire state)

    At each level:
      - If result count meets target → stop
      - If not → ask user if they want to expand radius
      - At 11z (state level) → inform user this is the maximum

    Returns deduplicated list of dealer dicts.
    """
    all_dealers = []
    seen_ids    = set()

    for zoom in ZOOM_LEVELS:
        label = ZOOM_LABELS[zoom]

        # Don't ask for first zoom — just search immediately
        if zoom != ZOOM_LEVELS[0]:
            expand = input(
                f"\n  Only {len(all_dealers)} dealers found so far. "
                f"Expand search to {label}? (y/n) : "
            ).strip().lower()
            if expand != "y":
                print(f"  Stopping search at current radius.")
                break

        print(f"\n  Radius: {label}")
        result = search_dealers(lat, lng, query, zoom=zoom)

        if not result["success"]:
            print(f"  ✗ Search failed: {result['error']}")
            break

        # Merge new results, deduplicating by place_id
        new_dealers = parse_dealers(result["results"])
        added = 0
        for d in new_dealers:
            pid = d.get("place_id")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            all_dealers.append(d)
            added += 1

        print(f"  ✓ {added} new dealers added (total: {len(all_dealers)})")

        if len(all_dealers) >= target:
            print(f"  ✓ Target of {target} met.")
            break

        # At state level — maximum reached
        if zoom == ZOOM_LEVELS[-1]:
            print(f"\n  ℹ Entire state scanned. {len(all_dealers)} dealers found total.")
            print(f"  This is the maximum coverage available.")

    return all_dealers


# ─── DISPLAY FUNCTION ──────────────────────────────────────────────────────────

def display_dealers(dealers: list):
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

def save_to_json(dealers: list, city: str, pincode: str) -> Path:
    filename = f"dealers_{city.replace(' ', '_')}_{pincode}.json"
    filepath = Path(__file__).parent / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(dealers, f, indent=2, ensure_ascii=False)
    print(f"\n  ✓ Saved to {filename}")
    return filepath


# ─── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    print("=" * 60)
    print("DEALER SEARCH — Powered by SerpAPI + Google Maps")
    print("=" * 60)

    # Step 1: Get location from user
    city    = input("\nEnter city name : ").strip()
    pincode = input("Enter pincode   : ").strip()
    country = "Malaysia"

    # Step 2: Dealer type
    print("\nWhat type of dealers are you looking for?")
    print("  [1] Used car dealers")
    print("  [2] New car dealers")
    print("  [3] Both")
    type_choice = input("Enter choice (1/2/3) : ").strip()

    dealer_type_map = {"1": "used", "2": "new", "3": "both"}
    dealer_type     = dealer_type_map.get(type_choice, "used")
    search_query    = get_search_query(dealer_type)
    print(f"  ✓ Searching for: {search_query}")

    # Step 3: How many profiles needed
    raw_target = input("\nHow many dealer profiles do you need? : ").strip()
    target     = int(raw_target) if raw_target.isdigit() else 10

    # For small targets (≤7), collect 2x to allow ranking after enrichment
    if target <= 7:
        collect_target = target * 2
        print(f"\n  Small target detected ({target}). "
              f"Collecting up to {collect_target} dealers for better ranking after enrichment.")
    else:
        collect_target = target
        print(f"\n  Collecting {collect_target} dealers.")

    # Step 4: Geocode
    print(f"\nGeocoding '{city}, {pincode}, {country}'...")
    geo = geocode(city, pincode, country)

    if not geo["success"]:
        print(f"\n✗ Geocoding failed: {geo['error']}")
        exit(1)

    print(f"✓ Coordinates: {geo['lat']}, {geo['lng']}")

    # Step 5: Collect dealers with radius expansion if needed
    print()
    dealers = collect_dealers(geo["lat"], geo["lng"], search_query, collect_target)

    if not dealers:
        print("\n✗ No dealers found.")
        exit(1)

    # Step 6: Display
    display_dealers(dealers)

    # Step 7: Save to JSON and trigger Phase 2
    save = input("\nSave results to JSON? (y/n) : ").strip().lower()
    if save == "y":
        # If small target, note that JinaWeb will rank and trim after enrichment
        if target <= 7:
            print(f"\n  Note: {len(dealers)} dealers saved. "
                  f"After enrichment, top {target} by score will be ranked.")

        json_path = save_to_json(dealers, city, pincode)

        run_phase2 = input("\nRun Phase 2 enrichment now? (y/n) : ").strip().lower()
        if run_phase2 == "y":
            print("\nHanding off to JinaWeb...\n")
            import sys
            sys.path.append(str(Path(__file__).parent.parent / "Phase_two"))
            from JinaWeb import run
            run(json_path, requested=target, small_target=(target <= 7))