"""
search_logic.py — Phase 1 dealer search (job-aware, no input() calls)
"""

import requests
import os
import re
from pathlib import Path
from dotenv import load_dotenv

import sys
sys.path.append(str(Path(__file__).parent.parent / "Phase_One"))
from converting import geocode
from job_manager import Job

load_dotenv(Path(__file__).parent.parent / ".env")
SERPAPI_KEY  = os.getenv("SERPAPI_KEY")
SERPAPI_URL  = "https://serpapi.com/search"

ZOOM_LEVELS = ["14z", "13z", "12z", "11z"]
ZOOM_LABELS = {
    "14z": "immediate area",
    "13z": "wider neighbourhood",
    "12z": "full city",
    "11z": "entire state",
}

SKIP_DOMAINS = [
    "carlist.my", "mudah.my", "olx.com", "carbay.my",
    "carsome.my", "cars.com", "cardekho.com", "carwale.com",
]


def get_search_query(dealer_type: str) -> str:
    if dealer_type == "used":
        return "Used car dealer"
    elif dealer_type == "new":
        return "New car dealer"
    return "Car dealer"


def _search_dealers(lat: float, lng: float, query: str, zoom: str) -> dict:
    params = {
        "engine":  "google_maps",
        "q":       query,
        "ll":      f"@{lat},{lng},{zoom}",
        "type":    "search",
        "api_key": SERPAPI_KEY,
    }
    try:
        r = requests.get(SERPAPI_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return {"success": False, "error": data["error"]}
        return {"success": True, "results": data.get("local_results", [])}
    except requests.exceptions.Timeout:
        return {"success": False, "error": "SerpAPI request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}


def _parse_dealers(raw: list) -> list:
    cleaned  = []
    seen_ids = set()
    for item in raw:
        name     = item.get("title", "").strip()
        website  = item.get("website")
        place_id = item.get("place_id")
        if not name:
            continue
        if place_id and place_id in seen_ids:
            continue
        if place_id:
            seen_ids.add(place_id)
        if website and any(d in website for d in SKIP_DOMAINS):
            continue
        cleaned.append({
            "name":     name,
            "address":  item.get("address", "").strip(),
            "phone":    item.get("phone"),
            "website":  website,
            "rating":   item.get("rating"),
            "place_id": place_id,
            "type":     item.get("type", ""),
        })
    return cleaned


def run_phase1(job: Job) -> list:
    """
    Runs Phase 1 for a job. Pauses at each zoom expansion and waits for
    job.expand_event to be set (by /search/respond endpoint).
    Returns list of dealer dicts.
    """
    query = get_search_query(job.dealer_type)
    collect_target = job.target * 2 if job.small_target else job.target

    job.log(f"Geocoding '{job.city}, {job.pincode}, Malaysia'...")
    geo = geocode(job.city, job.pincode, "Malaysia")

    if not geo["success"]:
        job.phase = "error"
        job.error = f"Geocoding failed: {geo['error']}"
        return []

    lat, lng = geo["lat"], geo["lng"]
    job.log(f"✓ Coordinates: {lat}, {lng}")
    job.log(f"Searching for: {query}")
    if job.small_target:
        job.log(f"Small target ({job.target}) — collecting up to {collect_target} dealers for ranking")

    all_dealers = []
    seen_ids    = set()

    for idx, zoom in enumerate(ZOOM_LEVELS):
        label = ZOOM_LABELS[zoom]

        # From second zoom onwards — ask user to confirm expansion
        if idx > 0:
            job.phase = "awaiting_expand"
            job.expand_prompt = {
                "found":     len(all_dealers),
                "next_area": label,
                "zoom":      zoom,
            }
            job.expand_event.clear()
            job.log(f"Waiting for expansion confirmation → {label}...")

            # Block until frontend responds (or job is cancelled)
            job.expand_event.wait()

            job.expand_prompt = None
            if not job.expand_answer:
                job.log("Expansion declined — stopping search.")
                job.phase = "searching"
                break

        job.phase = "searching"
        job.log(f"Searching radius: {label}...")

        result = _search_dealers(lat, lng, query, zoom)
        if not result["success"]:
            job.log(f"✗ Search failed: {result['error']}")
            break

        raw = result["results"]
        job.log(f"Got {len(raw)} results from Google Maps")

        new   = _parse_dealers(raw)
        added = 0
        for d in new:
            pid = d.get("place_id")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            all_dealers.append(d)
            added += 1

        job.log(f"✓ {added} new dealers added (total: {len(all_dealers)})")

        with job.dealers_lock:
            job.dealers = list(all_dealers)

        if len(all_dealers) >= collect_target:
            job.log(f"✓ Target of {collect_target} met.")
            break

        if zoom == ZOOM_LEVELS[-1]:
            job.log(f"Entire state scanned. {len(all_dealers)} dealers found total.")

    return all_dealers