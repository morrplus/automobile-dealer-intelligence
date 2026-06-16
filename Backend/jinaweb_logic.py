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
from rapidfuzz import fuzz

import job_context
from job_manager import Job
from emaillinkedin_logic import enrich_email_linkedin, crawl_website_for_all

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
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        r = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
        if r.status_code == 404:
            return False
        return True
    except requests.exceptions.ConnectionError:
        return False
    except Exception:
        return True


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
        if urls:
            return urls
        else:
            job_context.log_warning("        Jina returned 0 results, trying DuckDuckGo fallback")
    except Exception as e:
        job_context.log_warning(f"        Jina search failed ({e}), trying DuckDuckGo fallback")

    # Fallback to DuckDuckGo search
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
            
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
            urls = [r.get("href") for r in results if r.get("href")]
            job_context.log_info(f"        DuckDuckGo fallback returned {len(urls)} results")
            return urls
    except Exception as dde:
        job_context.log_warning(f"        DuckDuckGo fallback search also failed: {dde}")
        return []



def jina_search_detailed(query: str) -> list[dict]:
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
        out = []
        for item in results:
            if "url" in item:
                out.append({
                    "url": item["url"],
                    "title": item.get("title") or "",
                    "snippet": item.get("description") or item.get("content") or ""
                })
        job_context.log_info(f"        Jina returned {len(out)} results")
        if out:
            return out
        else:
            job_context.log_warning("        Jina returned 0 results, trying DuckDuckGo fallback")
    except Exception as e:
        job_context.log_warning(f"        Jina search failed ({e}), trying DuckDuckGo fallback")

    # Fallback to DuckDuckGo search
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
            
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
            out = []
            for r in results:
                url = r.get("href") or r.get("url")
                if url:
                    out.append({
                        "url": url,
                        "title": r.get("title") or "",
                        "snippet": r.get("body") or r.get("snippet") or ""
                    })
            job_context.log_info(f"        DuckDuckGo fallback returned {len(out)} results")
            return out
    except Exception as dde:
        job_context.log_warning(f"        DuckDuckGo fallback search also failed: {dde}")
        return []


def validate_social_profile(url: str, title: str, snippet: str, business_name: str, city: str, phone: str = None) -> bool:
    # 1. If phone number is found in the search result title, snippet, or URL, it's a guaranteed match!
    if phone:
        clean_phone = "".join(filter(str.isdigit, phone))
        # Keep last 7-9 digits of phone
        if len(clean_phone) >= 7:
            suffix = clean_phone[-7:]
            if suffix in title or suffix in snippet or suffix in url:
                return True

    # 2. Extract unique and descriptor words from the business name
    DESCRIPTOR_WORDS = {
        "auto", "motor", "motors", "car", "cars", "dealer", "dealership", 
        "trading", "enterprise", "group", "holdings", "credit", "sales", 
        "service", "services", "motorsport", "garage", "world", "empire", 
        "sdn", "bhd", "selection", "vehicles", "used", "new", "recond"
    }
    
    name_lower = business_name.lower()
    city_lower = city.lower() if city else ""
    
    # Check if slug or title/snippet mentions the city
    has_city = city_lower and (city_lower in url.lower() or city_lower in title.lower() or city_lower in snippet.lower())
    
    # Find descriptor words present in this business name
    descriptors_in_name = [w for w in DESCRIPTOR_WORDS if w in name_lower]
    
    # Extract slug/username from URL
    domain = ""
    for d in ("facebook.com", "instagram.com", "tiktok.com", "mudah.my", "carlist.my", "autocari.com"):
        if d in url.lower():
            domain = d
            break
            
    slug = ""
    if domain:
        slug = extract_slug(url, domain)
    
    # If the business name has descriptor words
    if descriptors_in_name:
        # Check if the slug contains at least one of these descriptors or the city
        has_descriptor_in_slug = any(d in slug.lower() for d in descriptors_in_name)
        has_descriptor_in_text = any(d in title.lower() or d in snippet.lower() for d in descriptors_in_name)
        
        # If the slug has no descriptor and no city, and the text has no descriptor and no city:
        # It's likely a personal profile or a mismatched company
        if not (has_descriptor_in_slug or has_city) and not (has_descriptor_in_text or has_city):
            return False

    # 3. Ensure the core unique words are present
    # Filter out generic/descriptor words to find distinct words
    words = re.findall(r"\b\w{3,}\b", name_lower)
    distinct_words = [w for w in words if w not in DESCRIPTOR_WORDS and w != city_lower]
    
    if distinct_words:
        # Check if at least one distinct word is in the slug or title/snippet
        slug_lower = slug.lower() if slug else url.lower()
        has_distinct = any(w in slug_lower or w in title.lower() or w in snippet.lower() for w in distinct_words)
        if not has_distinct:
            return False
            
    return True


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
            if parts:
                if parts[0] in ("p", "people") and len(parts) >= 2:
                    return parts[1].lower().replace(".", " ")
                if parts[0] not in ("groups", "pages", "events",
                                              "photo", "video", "watch",
                                              "posts", "accounts"):
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
    
    ratio = fuzz.token_set_ratio(cleaned, slug_clean) / 100.0
    job_context.log_info(f"        Slug match (token_set_ratio): '{slug_clean}' vs '{cleaned}' -> {ratio:.2f}")
    
    if ratio >= 0.85:
        return "high"
    if ratio >= 0.60:
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


def validate_website_domain(url: str, business_name: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        cleaned = clean_business_name(business_name)
        generic_words = {
            "auto", "motors", "motor", "car", "cars", "dealer", "dealership",
            "sdn", "bhd", "trading", "enterprise", "group", "holdings",
            "selection", "credit", "sales", "service", "services", "malaysia"
        }
        words = [w for w in cleaned.split() if len(w) >= 3 and w not in generic_words]
        if not words:
            return True
        for w in words:
            if w in domain:
                return True
        return False
    except Exception:
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


def find_platform_url(name: str, city: str, platform: dict, existing_url: str = None, phone: str = None) -> dict:
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
            if not validate_website_domain(url, name):
                job_context.log_info(f"    [website] ✗ Rejected mismatched search domain: {url}")
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

    is_social = key in ("facebook", "instagram", "tiktok")

    if is_social:
        for item in jina_search_detailed(query):
            url = item["url"]
            if domain not in url.lower():
                continue
            if not is_valid_structure(url):
                continue
            
            # Perform profile mismatch validation
            if not validate_social_profile(url, item["title"], item["snippet"], name, city, phone):
                job_context.log_info(f"    [{key}] ✗ Rejected mismatched profile: {url}")
                continue
                
            slug       = extract_slug(url, domain)
            confidence = slug_confidence(name, slug)
            
            # Clean Jina search results to return direct profile links instead of subpages/videos/posts
            if key == "facebook" and slug:
                if "profile.php" in url:
                    url_clean = url.rstrip("/")
                    # Keep only the profile.php?id=... part if present
                    if "id=" in url:
                        from urllib.parse import urlparse, parse_qs
                        try:
                            parsed_url = urlparse(url)
                            qs = parse_qs(parsed_url.query)
                            pid = qs.get("id")
                            if pid:
                                url_clean = f"https://www.facebook.com/profile.php?id={pid[0]}"
                        except Exception:
                            pass
                    url = url_clean
                elif "/p/" in url.lower():
                    parts = urlparse(url).path.strip("/").split("/")
                    if len(parts) >= 2:
                        url = f"https://www.facebook.com/p/{parts[1]}/"
                elif "/people/" in url.lower():
                    parts = urlparse(url).path.strip("/").split("/")
                    if len(parts) >= 3:
                        url = f"https://www.facebook.com/people/{parts[1]}/{parts[2]}/"
                    elif len(parts) >= 2:
                        url = f"https://www.facebook.com/people/{parts[1]}/"
                else:
                    url = f"https://www.facebook.com/{slug.replace(' ', '.')}/"
            elif key == "instagram" and slug:
                url = f"https://www.instagram.com/{slug.replace(' ', '.')}/"
            elif key == "tiktok" and slug:
                url = f"https://www.tiktok.com/@{slug.replace(' ', '.')}/"

            job_context.log_info(f"    [{key}] ✓ Found: {url} [{confidence} confidence]")
            return {"url": url, "confidence": confidence}
    else:
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

    # First, find/verify website (the first item in PLATFORMS)
    links = {}
    website_platform = PLATFORMS[0]
    website_res = find_platform_url(name, city, website_platform, existing_url=existing_url, phone=business.get("phone"))
    links["website"] = website_res
    
    website_url = website_res.get("url")
    crawled_emails = None
    crawled_phones = None
    crawled_linkedin = None
    crawled_socials = {}
    
    if website_url:
        crawl_res = crawl_website_for_all(website_url)
        crawled_emails = crawl_res.get("emails", [])
        crawled_phones = crawl_res.get("phones", [])
        crawled_socials = crawl_res.get("socials", {})
        crawled_linkedin = crawled_socials.get("linkedin")
        
        if crawled_emails:
            job_context.log_info(f"    [crawl] Extracted emails from website: {crawled_emails}")
        if crawled_phones:
            job_context.log_info(f"    [crawl] Extracted alternate phones from website: {crawled_phones}")
        if crawled_socials:
            job_context.log_info(f"    [crawl] Extracted social links from website: {crawled_socials}")

    # Process the remaining platforms
    for platform in PLATFORMS[1:]:
        key = platform["key"]
        if crawled_socials.get(key):
            found_url = crawled_socials[key]
            job_context.log_info(f"    [{key}] Skipping search — found on website: {found_url}")
            links[key] = {"url": found_url, "confidence": "high"}
        else:
            result = find_platform_url(name, city, platform, existing_url=None, phone=business.get("phone"))
            links[key] = result
            time.sleep(SLEEP_BETWEEN)

    scoring = calculate_score(links)
    job_context.log_info(f"  Score: {scoring['score']}/{scoring['max_score']}")

    enriched          = business.copy()
    enriched["links"] = links
    enriched["scoring"] = scoring
    
    if crawled_emails is not None:
        enriched["emails"] = crawled_emails
    if crawled_phones is not None:
        enriched["phones_extra"] = crawled_phones
    if crawled_linkedin is not None:
        enriched["linkedin"] = crawled_linkedin
        
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
    # If small_target, show progress against the user's requested number (not the bigger pool)
    display_total     = job.target if (job.small_target and job.target) else total
    job.enrich_total  = display_total
    job.enrich_done   = min(len(already_done), display_total)

    for i, business in enumerate(dealers, 1):
        pid = business.get("place_id", "")
        if pid in already_done:
            job.log(f"[{i}/{total}] Skipping (already done): {business.get('name')}")
            enriched = already_done[pid]
            with job.dealers_lock:
                for idx, d in enumerate(job.dealers):
                    if d.get("place_id") == pid:
                        job.dealers[idx] = enriched
                        break
                else:
                    job.dealers.append(enriched)
            continue

        job.log(f"\n[{i}/{total}] ══════════════════════")
        job.enrich_current = business.get("name")

        enriched = enrich_business(business)
        enriched = enrich_email_linkedin(enriched)

        results.append(enriched)
        # Cap display progress at the user's requested target
        job.enrich_done = min(len(results), display_total)

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

        # Save to Supabase immediately in real-time
        try:
            import supabase_client
            if supabase_client.is_configured():
                db_res = supabase_client.upsert_dealers([enriched], job.city, job.pincode, job.dealer_type)
                if db_res.get("inserted", 0) > 0:
                    job.log(f"  ✓ Saved to Supabase")
                if db_res.get("errors"):
                    for err in db_res["errors"]:
                        job.log(f"  ⚠ Supabase error: {err['error']}")
        except Exception as se:
            job.log(f"  ⚠ Supabase real-time save failed: {se}")

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
