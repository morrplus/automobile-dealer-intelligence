import requests
import json
import time
import os
import logging
import re
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import quote, urlparse
from difflib import SequenceMatcher

# ─── ENV SETUP ─────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent.parent / ".env")
JINA_API_KEY = os.getenv("JINA_API_KEY")

# ─── CONFIG ────────────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).parent / "jinaweb.log"

JINA_SEARCH_URL  = "https://s.jina.ai/"
SLEEP_BETWEEN    = 2
REQUEST_TIMEOUT  = 6
JINA_TIMEOUT     = 25
MIN_PAGE_LENGTH  = 500

# ─── PLATFORM DEFINITIONS ──────────────────────────────────────────────────────

PLATFORMS = [
    {"key": "website",   "keyword": None,               "domain": None},
    {"key": "mudah",     "keyword": "mudah",             "domain": "mudah.my"},
    {"key": "carlist",   "keyword": "carlist",           "domain": "carlist.my"},
    {"key": "autocari",  "keyword": "autocari",          "domain": "autocari.com"},
    {"key": "facebook",  "keyword": "facebook profile",  "domain": "facebook.com"},
    {"key": "instagram", "keyword": "instagram profile", "domain": "instagram.com"},
    {"key": "tiktok",    "keyword": "tiktok profile",    "domain": "tiktok.com"},
]

# ─── SCORING — points awarded per platform when a URL is found ─────────────────
PLATFORM_SCORES = {
    "website":   20,
    "mudah":     15,
    "carlist":   15,
    "autocari":  10,
    "facebook":  10,
    "instagram": 10,
    "tiktok":     5,
}
MAX_SCORE = sum(PLATFORM_SCORES.values())  # 85

WEBSITE_BLACKLIST = [
    "google.com", "maps.google", "wikipedia.org", "youtube.com",
    "facebook.com", "instagram.com", "tiktok.com", "mudah.my",
    "carlist.my", "autocari.com", "motortrader.com.my", "carsome.my",
    "mytukar.com", "waze.com", "foursquare.com", "tripadvisor.com",
    "twitter.com", "linkedin.com",
]

SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "tiktok.com")

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── HELPERS ───────────────────────────────────────────────────────────────────

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
        log.info(f"        Jina returned {len(urls)} results")
        return urls
    except Exception as e:
        log.warning(f"        Jina search failed: {e}")
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
        log.info(f"        Jina returned {len(out)} results")
        if out:
            return out
        else:
            log.warning("        Jina returned 0 results, trying DuckDuckGo fallback")
    except Exception as e:
        log.warning(f"        Jina search failed: {e}")

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
            log.info(f"        DuckDuckGo fallback returned {len(out)} results")
            return out
    except Exception as dde:
        log.warning(f"        DuckDuckGo fallback search also failed: {dde}")
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
    ratio = SequenceMatcher(None, cleaned, slug_clean).ratio()
    log.info(f"        Slug match: '{slug_clean}' vs '{cleaned}' → {ratio:.2f}")
    name_words    = [w for w in cleaned.split() if len(w) >= 4]
    matched_words = [w for w in name_words if w in slug_clean]
    log.info(f"        Word matches: {matched_words}")
    if ratio >= 0.6 or len(matched_words) >= 2:
        return "high"
    if ratio >= 0.35 or len(matched_words) >= 1:
        return "mid"
    return "low"


def is_valid_structure(url: str) -> bool:
    """
    Rejects structurally bad URLs for all platforms.
    Instagram is now treated identically to Facebook — posts and reels rejected.
    """
    url_lower = url.lower()
    bad_patterns = [
        # Instagram — strict (same as Facebook)
        r"instagram\.com/p/",
        r"instagram\.com/reel/",
        r"instagram\.com/reels/",
        r"instagram\.com/accounts/",
        r"instagram\.com/explore/",
        # Facebook
        r"facebook\.com/groups/",
        r"facebook\.com/events/",
        r"facebook\.com/photo",
        r"facebook\.com/watch",
        r"facebook\.com/posts/",
        # TikTok
        r"tiktok\.com/.+/video/",
        r"tiktok\.com/discover/",
        # Marketplace generic pages
        r"mudah\.my/$",
        r"carlist\.my/$",
        r"autocari\.com/index\.php\?r=dealer/(used|recond)&state",
    ]
    for pattern in bad_patterns:
        if re.search(pattern, url_lower):
            log.info(f"        Rejected (structural): {url}")
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
    """
    Calculates a completeness score for a business based on which links were found.
    Returns {"score": int, "max_score": int, "breakdown": {platform: points}}
    """
    score     = 0
    breakdown = {}

    for platform, points in PLATFORM_SCORES.items():
        entry = links.get(platform, {})
        if entry and entry.get("url"):
            breakdown[platform] = points
            score += points
        else:
            breakdown[platform] = 0

    return {
        "score":     score,
        "max_score": MAX_SCORE,
        "breakdown": breakdown,
    }


def find_platform_url(name: str, city: str, platform: dict,
                      existing_url: str = None, phone: str = None) -> dict:
    key     = platform["key"]
    domain  = platform["domain"]
    keyword = platform["keyword"]

    # ── Own website ──────────────────────────────────────────────────────────
    if key == "website":
        if existing_url:
            if any(s in existing_url.lower() for s in ["facebook.com", "instagram.com"]):
                log.info(f"    [website] Existing is a social URL — skipping as website")
            else:
                log.info(f"    [website] Checking existing: {existing_url}")
                if check_website(existing_url):
                    log.info(f"    [website] ✓ Verified existing")
                    return {"url": existing_url, "confidence": None}
                log.info(f"    [website] ✗ Dead or blank — searching for replacement")

        query = f"{name} {city} Malaysia"
        log.info(f"    [website] Searching: {query}")
        for url in jina_search(query):
            if any(bad in url.lower() for bad in WEBSITE_BLACKLIST):
                continue
            if not validate_website_domain(url, name):
                log.info(f"    [website] ✗ Rejected mismatched search domain: {url}")
                continue
            log.info(f"    [website] ✓ Found: {url}")
            return {"url": url, "confidence": None}

        log.info(f"    [website] ✗ Not found")
        return {"url": None, "confidence": None}

    # ── Marketplace (mudah, carlist, autocari) ───────────────────────────────
    if key in ("mudah", "carlist", "autocari"):
        query = f"{name} {city} {keyword}"
        log.info(f"    [{key}] Searching: {query}")
        for url in jina_search(query):
            if domain not in url.lower():
                continue
            if not is_valid_structure(url):
                continue
            log.info(f"    [{key}] ✓ Found: {url}")
            return {"url": url, "confidence": None}

        log.info(f"    [{key}] ✗ Not found")
        return {"url": None, "confidence": None}

    # ── Social platforms (facebook, instagram, tiktok) ───────────────────────
    # Instagram is now fully strict — same rules as Facebook, no exceptions
    query = f"{name} {city} {keyword}"
    log.info(f"    [{key}] Searching: {query}")

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
                log.info(f"    [{key}] ✗ Rejected mismatched profile: {url}")
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

            log.info(f"    [{key}] ✓ Found: {url} [{confidence} confidence]")
            return {"url": url, "confidence": confidence}
    else:
        for url in jina_search(query):
            if domain not in url.lower():
                continue
            if not is_valid_structure(url):
                continue
            slug       = extract_slug(url, domain)
            confidence = slug_confidence(name, slug)
            log.info(f"    [{key}] ✓ Found: {url} [{confidence} confidence]")
            return {"url": url, "confidence": confidence}

    log.info(f"    [{key}] ✗ Not found")
    return {"url": None, "confidence": None}


def enrich_business(business: dict) -> dict:
    name         = business.get("name", "")
    address      = business.get("address", "")
    existing_url = business.get("website")
    city         = extract_city(address)

    log.info(f"  Business : {name}")
    log.info(f"  City     : {city}")

    links = {}
    for platform in PLATFORMS:
        key    = platform["key"]
        eu     = existing_url if key == "website" else None
        result = find_platform_url(name, city, platform, existing_url=eu, phone=business.get("phone"))
        links[key] = result
        time.sleep(SLEEP_BETWEEN)

    # Calculate score based on what was found
    scoring = calculate_score(links)
    log.info(f"  Score: {scoring['score']}/{scoring['max_score']} — {scoring['breakdown']}")

    enriched = business.copy()
    enriched["links"]   = links
    enriched["scoring"] = scoring
    return enriched


def save_json(data: list, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── CORE LOGIC ────────────────────────────────────────────────────────────────

def main(input_json: Path, output_json: Path):
    from EmailLinkedin import enrich_email_linkedin

    if not input_json.exists():
        log.error(f"Input file not found: {input_json}")
        return

    with open(input_json, "r", encoding="utf-8") as f:
        businesses = json.load(f)

    total = len(businesses)
    log.info(f"Loaded {total} businesses from {input_json.name}")

    already_done = {}
    if output_json.exists():
        with open(output_json, "r", encoding="utf-8") as f:
            done_list = json.load(f)
        # Done only if BOTH links AND emails are present
        already_done = {
            b["place_id"]: b
            for b in done_list
            if "links" in b and "emails" in b
        }
        log.info(f"Resuming — {len(already_done)} already done, skipping them")

    results = list(already_done.values())

    for i, business in enumerate(businesses, 1):
        pid = business.get("place_id", "")

        if pid in already_done:
            log.info(f"[{i}/{total}] Skipping: {business.get('name')}")
            continue

        log.info(f"\n[{i}/{total}] ══════════════════════════════════════════")

        # Step A — JinaWeb: links + score
        enriched = enrich_business(business)

        # Step B — EmailLinkedin: emails + phones_extra + linkedin
        enriched = enrich_email_linkedin(enriched)

        results.append(enriched)
        save_json(results, output_json)
        log.info(f"  Saved progress → {output_json.name}")

    log.info(f"\n✓ Done. {len(results)} businesses written to {output_json}")


# ─── ENTRY POINTS ──────────────────────────────────────────────────────────────

def run(input_path: Path, requested: int = None, small_target: bool = False):
    """
    Called automatically by SearchMap.py after Phase 1 completes.

    requested    : how many profiles the user originally asked for
    small_target : if True, pool is larger than requested —
                   after enrichment sort by score and keep top requested
    """
    input_json  = Path(input_path)
    output_name = "dealers_enriched_" + input_json.stem.replace("dealers_", "") + ".json"
    output_json = Path(__file__).parent / output_name
    log.info(f"Input  : {input_json}")
    log.info(f"Output : {output_json}")

    main(input_json, output_json)

    # Small target — sort by score and trim to requested count
    if small_target and requested and output_json.exists():
        with open(output_json, "r", encoding="utf-8") as f:
            enriched = json.load(f)

        enriched.sort(
            key=lambda d: d.get("scoring", {}).get("score", 0),
            reverse=True
        )

        top = enriched[:requested]
        log.info(f"Ranking complete — keeping top {requested} of {len(enriched)} by score")

        ranked_name = output_json.stem + f"_top{requested}.json"
        ranked_path = output_json.parent / ranked_name
        with open(ranked_path, "w", encoding="utf-8") as f:
            json.dump(top, f, indent=2, ensure_ascii=False)

        log.info(f"Top {requested} saved → {ranked_path.name}")
        print(f"\n  ✓ Top {requested} dealers by profile score saved to {ranked_path.name}")


if __name__ == "__main__":
    print("=" * 60)
    print("JINAWEB — Phase 2 Enrichment (Standalone)")
    print("=" * 60)

    filename = input("\nEnter JSON filename from Phase_One (e.g. dealers_bangsar_59100.json) : ").strip()

    input_json  = Path(__file__).parent.parent / "Phase_One" / filename
    output_name = "dealers_enriched_" + filename.replace("dealers_", "")
    output_json = Path(__file__).parent / output_name

    if not input_json.exists():
        print(f"\n✗ File not found: {input_json}")
        exit(1)

    print(f"\n  Input  : {input_json}")
    print(f"  Output : {output_json}\n")
    main(input_json, output_json)