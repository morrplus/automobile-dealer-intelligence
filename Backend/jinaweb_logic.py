"""
jinaweb_logic.py — Phase 2 enrichment (job-aware, logging via job_context)
"""

import requests
import json
import time
import os
import re
from pathlib import Path
from urllib.parse import quote, urlparse
from difflib import SequenceMatcher
from dotenv import load_dotenv

import job_context
from job_manager import Job
from emaillinkedin_logic import enrich_email_linkedin

load_dotenv(Path(__file__).parent.parent / ".env")
JINA_API_KEY = os.getenv("JINA_API_KEY")

JINA_SEARCH_URL = "https://s.jina.ai/"
SLEEP_BETWEEN   = 2
REQUEST_TIMEOUT = 6
JINA_TIMEOUT    = 25
MIN_PAGE_LENGTH = 500

PLATFORMS = [
    {"key": "website",   "keyword": None,               "domain": None},
    {"key": "mudah",     "keyword": "mudah",             "domain": "mudah.my"},
    {"key": "carlist",   "keyword": "carlist",           "domain": "carlist.my"},
    {"key": "autocari",  "keyword": "autocari",          "domain": "autocari.com"},
    {"key": "facebook",  "keyword": "facebook profile",  "domain": "facebook.com"},
    {"key": "instagram", "keyword": "instagram profile", "domain": "instagram.com"},
    {"key": "tiktok",    "keyword": "tiktok profile",    "domain": "tiktok.com"},
]

PLATFORM_SCORES = {
    "website": 20, "mudah": 15, "carlist": 15,
    "autocari": 10, "facebook": 10, "instagram": 10, "tiktok": 5,
}
MAX_SCORE = sum(PLATFORM_SCORES.values())  # 85

WEBSITE_BLACKLIST = [
    "google.com", "maps.google", "wikipedia.org", "youtube.com",
    "facebook.com", "instagram.com", "tiktok.com", "mudah.my",
    "carlist.my", "autocari.com", "motortrader.com.my", "carsome.my",
    "mytukar.com", "waze.com", "foursquare.com", "tripadvisor.com",
    "twitter.com", "linkedin.com",
]


def check_website(url: str) -> bool:
    if not url:
        return False
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return False
        if len(r.text.strip()) < MIN_PAGE_LENGTH:
            return False
        return True
    except Exception:
        return False


def jina_search(query: str) -> list:
    encoded = quote(query)
    url     = f"{JINA_SEARCH_URL}{encoded}"
    headers = {"Accept": "application/json"}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    try:
        r = requests.get(url, headers=headers, timeout=JINA_TIMEOUT)
        r.raise_for_status()
        data    = r.json()
        results = data.get("data", [])
        urls    = [item["url"] for item in results if "url" in item]
        job_context.log_info(f"        Jina returned {len(urls)} results")
        return urls
    except Exception as e:
        job_context.log_warning(f"Jina search failed: {e}")
        return []


def extract_city(address: str) -> str:
    if not address:
        return "Malaysia"
    known_cities = [
        "Kuala Lumpur", "Petaling Jaya", "Shah Alam", "Subang Jaya",
        "Klang", "Ampang", "Cheras", "Puchong", "Cyberjaya", "Putrajaya",
        "Johor Bahru", "Penang", "Georgetown", "Ipoh", "Kota Kinabalu",
        "Kuching", "Malacca", "Seremban", "Alor Setar", "Kuantan",
        "Bangsar", "Mont Kiara", "Damansara", "Kepong", "Setapak",
    ]
    for city in known_cities:
        if city.lower() in address.lower():
            return city
    parts = [p.strip() for p in address.split(",")]
    for part in reversed(parts):
        if re.match(r"^\d+$", part):
            continue
        if part.lower() in ["malaysia", "wilayah persekutuan kuala lumpur",
                             "federal territory of kuala lumpur"]:
            continue
        if len(part) < 30:
            return part
    return "Malaysia"


def clean_business_name(name: str) -> str:
    noise = [
        r"\bsdn\.?\s*bhd\.?\b", r"\bsdn\b", r"\bbhd\b",
        r"\benterprise\b", r"\bgroup\b", r"\bholdings?\b",
        r"\(m\)", r"\bm\b", r"\bco\.?\b", r"\bltd\.?\b",
        r"\binternational\b", r"\bglobal\b", r"\bautomotive\b",
    ]
    result = name.lower()
    for pattern in noise:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    return result.strip()


def extract_slug(url: str, domain: str) -> str:
    try:
        path = urlparse(url).path.strip("/")
        if domain == "tiktok.com":
            match = re.match(r"@([\w.]+)", path)
            return match.group(1).lower() if match else ""
        if domain in ("facebook.com", "instagram.com"):
            parts = path.split("/")
            if parts and parts[0] not in ("groups", "pages", "events",
                                          "photo", "video", "watch",
                                          "posts", "accounts", "p"):
                return parts[0].lower().replace(".", " ")
            return ""
        if domain == "mudah.my":
            parts = path.split("/")
            slug  = parts[-1] if parts else ""
            return slug.replace("-", " ").lower()
        return path.split("/")[0].lower()
    except Exception:
        return ""


def slug_confidence(business_name: str, slug: str) -> str:
    if not slug:
        return "low"
    cleaned    = clean_business_name(business_name)
    slug_clean = slug.replace("-", " ").replace("_", " ").replace(".", " ")
    ratio      = SequenceMatcher(None, cleaned, slug_clean).ratio()
    job_context.log_info(f"        Slug match: '{slug_clean}' vs '{cleaned}' → {ratio:.2f}")
    name_words    = [w for w in cleaned.split() if len(w) >= 4]
    matched_words = [w for w in name_words if w in slug_clean]
    job_context.log_info(f"        Word matches: {matched_words}")
    if ratio >= 0.6 or len(matched_words) >= 2:
        return "high"
    if ratio >= 0.35 or len(matched_words) >= 1:
        return "mid"
    return "low"


def is_valid_structure(url: str) -> bool:
    url_lower = url.lower()
    bad_patterns = [
        r"instagram\.com/p/", r"instagram\.com/reel/", r"instagram\.com/reels/",
        r"instagram\.com/accounts/", r"instagram\.com/explore/",
        r"facebook\.com/groups/", r"facebook\.com/events/",
        r"facebook\.com/photo", r"facebook\.com/watch", r"facebook\.com/posts/",
        r"tiktok\.com/.+/video/", r"tiktok\.com/discover/",
        r"mudah\.my/$", r"carlist\.my/$",
        r"autocari\.com/index\.php\?r=dealer/(used|recond)&state",
    ]
    for pattern in bad_patterns:
        if re.search(pattern, url_lower):
            return False
    return True


def calculate_score(links: dict) -> dict:
    score     = 0
    breakdown = {}
    for platform, points in PLATFORM_SCORES.items():
        entry = links.get(platform, {})
        if entry and entry.get("url"):
            breakdown[platform] = points
            score += points
        else:
            breakdown[platform] = 0
    return {"score": score, "max_score": MAX_SCORE, "breakdown": breakdown}


def find_platform_url(name: str, city: str, platform: dict, existing_url: str = None) -> dict:
    key     = platform["key"]
    domain  = platform["domain"]
    keyword = platform["keyword"]

    if key == "website":
        if existing_url:
            if any(s in existing_url.lower() for s in ["facebook.com", "instagram.com"]):
                job_context.log_info(f"    [website] Existing is social — skipping")
            else:
                job_context.log_info(f"    [website] Checking existing: {existing_url}")
                if check_website(existing_url):
                    job_context.log_info(f"    [website] ✓ Verified existing")
                    return {"url": existing_url, "confidence": None}
                job_context.log_info(f"    [website] ✗ Dead — searching for replacement")

        query = f"{name} {city} Malaysia"
        job_context.log_info(f"    [website] Searching: {query}")
        for url in jina_search(query):
            if any(bad in url.lower() for bad in WEBSITE_BLACKLIST):
                continue
            job_context.log_info(f"    [website] ✓ Found: {url}")
            return {"url": url, "confidence": None}

        job_context.log_info(f"    [website] ✗ Not found")
        return {"url": None, "confidence": None}

    if key in ("mudah", "carlist", "autocari"):
        query = f"{name} {city} {keyword}"
        job_context.log_info(f"    [{key}] Searching: {query}")
        for url in jina_search(query):
            if domain not in url.lower():
                continue
            if not is_valid_structure(url):
                continue
            job_context.log_info(f"    [{key}] ✓ Found: {url}")
            return {"url": url, "confidence": None}

        job_context.log_info(f"    [{key}] ✗ Not found")
        return {"url": None, "confidence": None}

    query = f"{name} {city} {keyword}"
    job_context.log_info(f"    [{key}] Searching: {query}")
    for url in jina_search(query):
        if domain not in url.lower():
            continue
        if not is_valid_structure(url):
            continue
        slug       = extract_slug(url, domain)
        confidence = slug_confidence(name, slug)
        job_context.log_info(f"    [{key}] ✓ Found: {url} [{confidence} confidence]")
        return {"url": url, "confidence": confidence}

    job_context.log_info(f"    [{key}] ✗ Not found")
    return {"url": None, "confidence": None}


def enrich_business(business: dict) -> dict:
    name         = business.get("name", "")
    address      = business.get("address", "")
    existing_url = business.get("website")
    city         = extract_city(address)

    job_context.log_info(f"  Business: {name}")
    job_context.log_info(f"  City:     {city}")

    links = {}
    for platform in PLATFORMS:
        key    = platform["key"]
        eu     = existing_url if key == "website" else None
        result = find_platform_url(name, city, platform, existing_url=eu)
        links[key] = result
        time.sleep(SLEEP_BETWEEN)

    scoring = calculate_score(links)
    job_context.log_info(f"  Score: {scoring['score']}/{scoring['max_score']}")

    enriched          = business.copy()
    enriched["links"] = links
    enriched["scoring"] = scoring
    return enriched


def run_phase2(job: Job, dealers: list, data_dir: Path) -> list:
    """
    Runs Phase 2 enrichment for all dealers in a job.
    Updates job progress in real time.
    Returns fully enriched dealer list.
    """
    job_context.set_job(job)

    output_path = data_dir / f"dealers_enriched_{job.city.replace(' ', '_')}_{job.pincode}.json"

    # Resume support
    already_done = {}
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                done_list = json.load(f)
            already_done = {
                b["place_id"]: b
                for b in done_list
                if "links" in b and "emails" in b
            }
            job.log(f"Resuming — {len(already_done)} already done")
        except Exception:
            pass

    results = list(already_done.values())
    total   = len(dealers)
    job.enrich_total = total
    job.enrich_done  = len(already_done)

    for i, business in enumerate(dealers, 1):
        pid = business.get("place_id", "")
        if pid in already_done:
            job.log(f"[{i}/{total}] Skipping (already done): {business.get('name')}")
            continue

        job.log(f"\n[{i}/{total}] ══════════════════════")
        job.enrich_current = business.get("name")

        enriched = enrich_business(business)
        enriched = enrich_email_linkedin(enriched)

        results.append(enriched)
        job.enrich_done = len(results)

        # Update live dealers list in job
        with job.dealers_lock:
            for idx, d in enumerate(job.dealers):
                if d.get("place_id") == pid:
                    job.dealers[idx] = enriched
                    break
            else:
                job.dealers.append(enriched)

        # Save progress
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            job.log(f"  Saved progress → {output_path.name}")
        except Exception as e:
            job.log(f"  ⚠ Save failed: {e}")

    job.log(f"\n✓ Enrichment complete — {len(results)} dealers processed")

    # Small target ranking
    recommended_ids = []
    if job.small_target and job.target:
        results.sort(key=lambda d: d.get("scoring", {}).get("score", 0), reverse=True)
        top = results[: job.target]
        recommended_ids = [d.get("place_id") for d in top if d.get("place_id")]

        ranked_path = output_path.parent / (output_path.stem + f"_top{job.target}.json")
        try:
            with open(ranked_path, "w", encoding="utf-8") as f:
                json.dump(top, f, indent=2, ensure_ascii=False)
            job.log(f"✓ Top {job.target} saved → {ranked_path.name}")
        except Exception as e:
            job.log(f"⚠ Could not save ranked file: {e}")
    else:
        recommended_ids = [d.get("place_id") for d in results if d.get("place_id")]

    job.recommended_ids = recommended_ids
    return results
