"""
EmailLinkedin.py — Phase 2 Email + LinkedIn Enrichment
=======================================================
Runs after JinaWeb enriches each business.
Adds to the same output JSON:
  - emails     : list of emails found on website + contact pages
  - phones_extra: additional phones found on website (beyond SerpAPI)
  - linkedin   : linkedin.com/company URL or None

Tools used (all optional except requests + bs4):
  - requests + BeautifulSoup : crawl dealer website
  - Firecrawl                : JS-heavy site fallback (FIRECRAWL_API_KEY)
  - Hunter.io                : email by domain fallback (HUNTER_API_KEY)
  - DuckDuckGo (ddgs)        : LinkedIn URL discovery (free)

Install:
    pip install requests beautifulsoup4 lxml ddgs
    pip install python-dotenv  (already installed)
"""

import re
import os
import time
import logging
import threading
from pathlib import Path
from urllib.parse import urlparse
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ── DDG import ─────────────────────────────────────────────────────────────────
try:
    from ddgs import DDGS
    DDG_AVAILABLE = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        DDG_AVAILABLE = True
    except ImportError:
        DDG_AVAILABLE = False

# ─── ENV ───────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent.parent / ".env")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
HUNTER_API_KEY    = os.getenv("HUNTER_API_KEY")
PILOTERR_API_KEY  = os.getenv("PILOTERR_API_KEY")
JINA_API_KEY      = os.getenv("JINA_API_KEY")

# ─── CONFIG ────────────────────────────────────────────────────────────────────

REQUEST_TIMEOUT    = 8
FIRECRAWL_TIMEOUT  = 30
JS_SHELL_THRESHOLD = 2000   # chars — below this = JS-only page, try Firecrawl
MAX_PAGES_TO_CRAWL = 4      # homepage + up to 3 contact/about subpages
DDG_MAX_RESULTS    = 8
SLEEP_DDG          = 1.5    # seconds between DDG calls
JINA_TIMEOUT      = 25

FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"
HUNTER_URL    = "https://api.hunter.io/v2/domain-search"

_CRAWL_CACHE = {}
_CRAWL_CACHE_LOCK = threading.Lock()

# Subpages to try for contact info after homepage
CONTACT_PATHS = [
    "/contact", "/contact-us", "/contact_us", "/contactus",
    "/about", "/about-us", "/about_us",
    "/reach-us", "/hubungi-kami", "/team",
]

# Emails matching these patterns are noise — skip them
EMAIL_BLACKLIST = [
    r"example\.", r"@sentry\.", r"@google\.", r"noreply", r"no-reply",
    r"support@", r"privacy@", r"legal@", r"admin@", r"webmaster@",
    r"info@wixpress", r"@wix\.", r"@wordpress\.", r"@gravatar\.",
]

# Social/aggregator domains — never returned as dealer website
SOCIAL_DOMAINS = [
    "facebook.com", "instagram.com", "tiktok.com", "mudah.my",
    "carlist.my", "linkedin.com", "youtube.com", "twitter.com",
]

# LinkedIn slug validation — known foreign suffixes to reject
FOREIGN_LI_SUFFIXES = (
    "-au", "-uk", "-us", "-sg", "-id", "-ph",
    "-th", "-vn", "-cn", "-jp", "-kr", "-in",
)

# Regex
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I
)
MY_PHONE_RE = re.compile(
    r"(?:\+?60|0)[\s\-]?(?:1[0-9][\s\-]?\d{3,4}[\s\-]?\d{4}"
    r"|[3-9][\s\-]?\d{2,4}[\s\-]?\d{4})",
    re.I,
)
LINKEDIN_COMPANY_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9\-_%.]+)/?",
    re.I,
)

# ─── LOGGING ───────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)   # inherits JinaWeb's logging config

# ─── UTILITIES ─────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> str | None:
    """
    Fetches page HTML.
    Falls back to Firecrawl if response is a JS shell (< JS_SHELL_THRESHOLD chars).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    html = None
    try:
        r = requests.get(url, headers=headers,
                         timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code < 400:
            html = r.text
    except Exception:
        pass

    if html and len(html.strip()) >= JS_SHELL_THRESHOLD:
        return html

    # JS shell or failed — try Firecrawl if key available
    if FIRECRAWL_API_KEY:
        try:
            log.info(f"    [Firecrawl] JS shell — fetching: {url}")
            resp = requests.post(
                FIRECRAWL_URL,
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"url": url, "formats": ["html"], "onlyMainContent": False},
                timeout=FIRECRAWL_TIMEOUT,
            )
            if resp.status_code == 200:
                fc_html = resp.json().get("data", {}).get("html")
                if fc_html and len(fc_html.strip()) > (len(html.strip()) if html else 0):
                    log.info(f"    [Firecrawl] Got {len(fc_html)} chars")
                    return fc_html
        except Exception as e:
            log.warning(f"    [Firecrawl] Failed: {e}")

    return html


def is_clean_email(email: str) -> bool:
    """Returns True if email looks real — not a placeholder or system address."""
    el = email.lower()
    if any(re.search(p, el) for p in EMAIL_BLACKLIST):
        return False
    image_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
    if any(el.endswith(ext) for ext in image_exts):
        return False
    if re.search(r"\d+x\d+", el):
        return False
    return True


def extract_emails_and_phones(html: str) -> tuple[list[str], list[str]]:
    """
    Extracts emails and Malaysian phone numbers from page HTML.
    Prioritises mailto: links, then scans full text.
    Skips placeholder values (form examples).
    """
    soup = BeautifulSoup(html, "lxml")

    # Collect placeholder emails to exclude
    placeholder_emails: set[str] = set()
    for tag in soup.find_all(True, {"placeholder": True}):
        ph = tag.get("placeholder", "")
        if "@" in ph:
            m = EMAIL_RE.search(ph)
            if m:
                placeholder_emails.add(m.group(0).lower())

    # mailto: links
    mailto_emails: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            if addr and "@" in addr and addr not in placeholder_emails:
                mailto_emails.append(addr)

    # tel: and WhatsApp links
    tel_phones: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        h    = href.lower()
        if h.startswith("tel:"):
            num = href[4:].strip()
            if num:
                tel_phones.append(num)
        elif "wa.me/" in h or "whatsapp.com/send" in h:
            try:
                from urllib.parse import parse_qs
                parsed_wa = urlparse(href)
                qs = parse_qs(parsed_wa.query)
                phone_num = (
                    qs["phone"][0] if "phone" in qs
                    else parsed_wa.path.strip("/").split("/")[-1]
                )
                cleaned = re.sub(r"\D", "", phone_num)
                if cleaned.startswith("60") and 10 <= len(cleaned) <= 12:
                    tel_phones.append("0" + cleaned[2:])
                elif cleaned.startswith("0") and 9 <= len(cleaned) <= 11:
                    tel_phones.append(cleaned)
            except Exception:
                pass

    # Full text scan
    plain   = soup.get_text(separator=" ")
    combined = plain + " " + html
    combined = re.sub(r"\s*\[at\]\s*",  "@", combined, flags=re.I)
    combined = re.sub(r"\s*\[dot\]\s*", ".", combined, flags=re.I)
    combined = re.sub(r"\s*\(at\)\s*",  "@", combined, flags=re.I)
    combined = re.sub(r"\s*\(dot\)\s*", ".", combined, flags=re.I)

    regex_emails = [
        m.group(0).lower()
        for m in EMAIL_RE.finditer(combined)
        if m.group(0).lower() not in placeholder_emails
    ]
    plain_for_phone = plain.replace("(", "").replace(")", "")
    regex_phones    = [m.group(0) for m in MY_PHONE_RE.finditer(plain_for_phone)]

    # Merge and deduplicate
    all_emails = list(dict.fromkeys(mailto_emails + regex_emails))
    all_phones = list(dict.fromkeys(tel_phones + regex_phones))

    clean_emails = [e.strip() for e in all_emails if is_clean_email(e.strip())]
    return clean_emails, all_phones


# ─── EMAIL EXTRACTION ──────────────────────────────────────────────────────────

def extract_emails(website_url: str | None) -> tuple[list[str], list[str]]:
    res = crawl_website_for_all(website_url)
    return res["emails"], res["phones"]


# ─── LINKEDIN DISCOVERY ────────────────────────────────────────────────────────

def clean_facebook_url(url: str) -> str:
    if not url:
        return ""
    if "profile.php" in url:
        return url.rstrip("/")
    url = url.split("?")[0].rstrip("/")
    
    match = re.search(r"https?://(?:[a-z0-9\-]+\.)?facebook\.com/([^/]+)", url, re.I)
    if match:
        username = match.group(1).lower()
        if username in ("groups", "events", "photo.php", "permalink.php", "share", "watch", "reel", "sharer", "people", "p"):
            if username == "people":
                parts = urlparse(url).path.strip("/").split("/")
                if len(parts) >= 3:
                    return f"https://www.facebook.com/people/{parts[1]}/{parts[2]}/"
            elif username == "p":
                parts = urlparse(url).path.strip("/").split("/")
                if len(parts) >= 2:
                    return f"https://www.facebook.com/p/{parts[1]}/"
            return url
        return f"https://www.facebook.com/{username}/"
    return url


def extract_social_links(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    socials = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href_lower = href.lower()
        if "facebook.com" in href_lower or "fb.com" in href_lower:
            socials["facebook"] = clean_facebook_url(href)
        elif "instagram.com" in href_lower or "instagr.am" in href_lower:
            clean_insta = href.split("?")[0].rstrip("/")
            if "/p/" not in clean_insta.lower() and "/reel/" not in clean_insta.lower():
                socials["instagram"] = clean_insta
        elif "tiktok.com" in href_lower:
            clean_tiktok = href.split("?")[0].rstrip("/")
            if "/video/" not in clean_tiktok.lower() and "/discover/" not in clean_tiktok.lower():
                socials["tiktok"] = clean_tiktok
        elif "linkedin.com/company/" in href_lower or "linkedin.com/in/" in href_lower:
            clean_li = href.split("?")[0].rstrip("/")
            socials["linkedin"] = clean_li
    return socials


def crawl_website_for_all(website_url: str | None) -> dict:
    if not website_url:
        return {"emails": [], "phones": [], "socials": {}}
    
    with _CRAWL_CACHE_LOCK:
        if website_url in _CRAWL_CACHE:
            return _CRAWL_CACHE[website_url]
            
    if any(s in website_url.lower() for s in SOCIAL_DOMAINS):
        return {"emails": [], "phones": [], "socials": {}}
        
    log.info(f"    [crawl] Crawling website for socials + contacts: {website_url}")
    
    all_emails = []
    all_phones = []
    all_socials = {}
    visited = set()
    
    def crawl(url: str):
        if url in visited or len(visited) >= MAX_PAGES_TO_CRAWL:
            return
        visited.add(url)
        log.info(f"    [crawl] Page: {url}")
        html = fetch_page(url)
        if not html:
            return
            
        emails, phones = extract_emails_and_phones(html)
        for e in emails:
            if e not in all_emails:
                all_emails.append(e)
        for p in phones:
            if p not in all_phones:
                all_phones.append(p)
                
        socials = extract_social_links(html)
        for k, v in socials.items():
            if v and k not in all_socials:
                all_socials[k] = v
                
    crawl(website_url)
    base = website_url.rstrip("/")
    for path in CONTACT_PATHS:
        if len(visited) >= MAX_PAGES_TO_CRAWL:
            break
        crawl(base + path)
        time.sleep(0.5)
        
    if not all_emails and HUNTER_API_KEY:
        try:
            domain = urlparse(website_url).netloc.lstrip("www.")
            if domain and "." in domain and not any(s in domain for s in SOCIAL_DOMAINS):
                log.info(f"    [email] Hunter.io fallback for: {domain}")
                resp = requests.get(
                    HUNTER_URL,
                    params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 5},
                    timeout=10,
                )
                if resp.status_code == 200:
                    hunter_emails = [
                        e["value"] for e in resp.json().get("data", {}).get("emails", [])
                        if e.get("value") and is_clean_email(e["value"])
                    ]
                    if hunter_emails:
                        log.info(f"    [email] Hunter.io found: {hunter_emails}")
                        all_emails.extend(hunter_emails)
        except Exception as e:
            log.warning(f"Hunter.io failed: {e}")
            
    res = {
        "emails": all_emails,
        "phones": all_phones,
        "socials": all_socials
    }
    
    with _CRAWL_CACHE_LOCK:
        _CRAWL_CACHE[website_url] = res
        
    return res


def extract_emails_from_facebook(fb_url: str, business_name: str = "") -> list[str]:
    if not fb_url:
        return []
    
    # Extract unique words to verify the page actually belongs to the business
    name_clean = business_name.lower()
    # Filter out generic words to find distinct words
    words = re.findall(r"\b\w{3,}\b", name_clean)
    generic = {"auto", "motor", "motors", "car", "cars", "dealer", "dealership", "trading", "enterprise", "group", "holdings", "sdn", "bhd", "malaysia"}
    distinct_words = [w for w in words if w not in generic]
    
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            jina_url = f"https://r.jina.ai/{fb_url}"
            headers = {"Accept": "application/json"}
            if JINA_API_KEY:
                headers["Authorization"] = f"Bearer {JINA_API_KEY}"
            
            # Use X-No-Cache to force proxy rotation if we failed previously
            if attempt > 1:
                headers["X-No-Cache"] = "true"
                
            resp = requests.get(jina_url, headers=headers, timeout=JINA_TIMEOUT)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    content = data.get("data", {}).get("content", "") or data.get("content", "") or resp.text
                except Exception:
                    content = resp.text
                
                content_lower = content.lower()
                # Check for Facebook login/redirect blocks
                is_login_page = "log into facebook" in content_lower or "explore the things you love" in content_lower or "create new account" in content_lower
                
                # Clean content to strip Markdown links and absolute URLs to prevent matching key words in URL parameters
                content_clean = re.sub(r'\[.*?\]\(.*?\)', '', content_lower)
                content_clean = re.sub(r'https?://\S+', '', content_clean)
                
                # If it's a login page, and we have distinct words but none are present in content:
                has_business = True
                if distinct_words:
                    has_business = any(w in content_clean for w in distinct_words)
                
                if is_login_page and not has_business:
                    if attempt < max_retries:
                        log.info(f"    [email] Facebook scrape returned login redirect on attempt {attempt}/{max_retries}. Retrying with fresh proxy...")
                        time.sleep(2.0)
                        continue
                    else:
                        log.info("    [email] Facebook scrape consistently blocked by login redirect after all attempts.")
                
                found = EMAIL_RE.findall(content)
                clean_found = [e.lower().strip() for e in found if is_clean_email(e.strip())]
                return list(dict.fromkeys(clean_found))
                
            elif resp.status_code == 429:
                if attempt < max_retries:
                    log.info(f"    [email] Jina rate limit (429) on attempt {attempt}/{max_retries}. Retrying...")
                    time.sleep(2.5)
                    continue
            else:
                log.info(f"    [email] Jina returned status {resp.status_code} for Facebook page.")
                
        except Exception as e:
            if attempt < max_retries:
                log.info(f"    [email] Facebook scrape error on attempt {attempt}/{max_retries}: {e}. Retrying...")
                time.sleep(2.0)
                continue
            else:
                log.warning(f"Facebook email extraction failed: {e}")
                

def search_email_via_jina(business_name: str) -> list[str]:
    from urllib.parse import quote
    query = f'"{business_name}" email OR contact'
    encoded = quote(query)
    url = f"https://s.jina.ai/{encoded}"
    headers = {"Accept": "application/json"}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    try:
        resp = requests.get(url, headers=headers, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("data", [])
            found = []
            for item in results:
                snippet = item.get("description", "") or item.get("content", "") or ""
                matches = EMAIL_RE.findall(snippet)
                for m in matches:
                    clean = m.lower().strip()
                    if is_clean_email(clean) and clean not in found:
                        found.append(clean)
            return found
    except Exception as e:
        log.warning(f"Direct search email fallback failed: {e}")
    return []




def lookup_linkedin_by_domain(domain: str) -> str | None:
    if not PILOTERR_API_KEY or not domain:
        return None
    try:
        resp = requests.get(
            "https://api.piloterr.com/v2/linkedin/company/info",
            params={"domain": domain},
            headers={"x-api-key": PILOTERR_API_KEY},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            url = data.get("company_url")
            if url:
                return url.split("?")[0].rstrip("/")
    except Exception as e:
        log.warning(f"Piloterr lookup failed: {e}")
    return None


def verify_linkedin_by_domain(linkedin_url: str, website_domain: str, business_name: str = "") -> bool:
    if not linkedin_url or not website_domain:
        return False
    try:
        jina_url = f"https://r.jina.ai/{linkedin_url}"
        headers = {"Accept": "text/plain"}
        if JINA_API_KEY:
            headers["Authorization"] = f"Bearer {JINA_API_KEY}"
        resp = requests.get(jina_url, headers=headers, timeout=20)
        if resp.status_code == 200:
            content = resp.text.lower()
            clean_domain = website_domain.lower().replace("www.", "")
            if clean_domain in content:
                log.info(f"    [linkedin] Jina validation SUCCESS: {clean_domain} found on LinkedIn page")
                return True
            else:
                log.info(f"    [linkedin] Jina validation FAILED: {clean_domain} not found on LinkedIn page")
                return False
        else:
            log.warning(f"    [linkedin] Jina validation got status code {resp.status_code} for {linkedin_url}. Falling back to name-slug verification.")
            if business_name:
                return validate_linkedin_slug(linkedin_url, business_name)
    except Exception as e:
        log.warning(f"    [linkedin] Jina validation failed: {e}. Falling back to name-slug verification.")
        if business_name:
            return validate_linkedin_slug(linkedin_url, business_name)
    return False


def clean_business_name_li(name: str) -> str:
    """Strip legal suffixes for cleaner LinkedIn slug matching."""
    noise = [
        r"\bsdn\.?\s*bhd\.?\b", r"\bsdn\b", r"\bbhd\b",
        r"\benterprise\b", r"\bgroup\b", r"\bholdings?\b",
        r"\(m\)", r"\bco\.?\b", r"\bltd\.?\b",
        r"\binternational\b", r"\bglobal\b", r"\bautomotive\b",
        r"\bauto\b", r"\bmotors?\b",
    ]
    result = name.lower()
    for pattern in noise:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    return result.strip()


def validate_linkedin_slug(url: str, business_name: str) -> bool:
    """
    Returns True if the LinkedIn URL is likely the correct Malaysian company.
    Rejects foreign country suffixes and slug mismatches.
    """
    try:
        m = LINKEDIN_COMPANY_RE.search(url)
        if not m:
            return False
        slug = m.group(1).lower()

        # Reject foreign country-code suffixes
        if any(slug.endswith(s) for s in FOREIGN_LI_SUFFIXES):
            log.info(f"    [linkedin] Rejected — foreign suffix: {slug}")
            return False

        # Reject foreign subdomains (au.linkedin.com, uk.linkedin.com etc.)
        parsed  = urlparse(url)
        subdomain = parsed.netloc.lower().split(".")[0]
        if subdomain not in ("www", "my", "linkedin"):
            log.info(f"    [linkedin] Rejected — foreign subdomain: {subdomain}")
            return False

        # Slug must share meaningful words with business name
        cleaned    = clean_business_name_li(business_name)
        slug_words = slug.replace("-", " ").split()
        name_words = [w for w in cleaned.split() if len(w) >= 4]

        ratio     = SequenceMatcher(None, cleaned, slug.replace("-", " ")).ratio()
        word_hits = [w for w in name_words if w in slug_words]

        min_hits = 2 if len(name_words) <= 2 else 1
        if ratio >= 0.55 and len(word_hits) >= min_hits:
            return True

        log.info(
            f"    [linkedin] Rejected slug (ratio={ratio:.2f}, hits={word_hits}): {slug}"
        )
        return False
    except Exception:
        return True


def find_linkedin(name: str, city: str, website_url: str | None = None) -> str | None:
    # Layer 1: Already handled by extracting from website crawl
    if website_url:
        cached = crawl_website_for_all(website_url)
        li_from_web = cached.get("socials", {}).get("linkedin")
        if li_from_web:
            if validate_linkedin_slug(li_from_web, name):
                log.info(f"    [linkedin] Layer 1: Found direct link on website: {li_from_web}")
                return li_from_web
            else:
                log.info(f"    [linkedin] Layer 1: Rejected website LinkedIn link (failed slug validation): {li_from_web}")

    # Resolve domain if website is available
    domain = ""
    if website_url:
        try:
            domain = urlparse(website_url).netloc.lower().replace("www.", "")
        except Exception:
            pass

    # Layer 2: Piloterr domain-to-LinkedIn API
    if domain:
        log.info(f"    [linkedin] Layer 2: Querying Piloterr for domain: {domain}")
        piloterr_li = lookup_linkedin_by_domain(domain)
        if piloterr_li:
            log.info(f"    [linkedin] Piloterr returned: {piloterr_li}")
            if validate_linkedin_slug(piloterr_li, name):
                if verify_linkedin_by_domain(piloterr_li, domain, name):
                    log.info(f"    [linkedin] ✓ Verified Piloterr link via Jina: {piloterr_li}")
                    return piloterr_li
            else:
                log.info(f"    [linkedin] Piloterr link rejected (failed slug validation): {piloterr_li}")

    # Layer 3: Dual DuckDuckGo queries consensus
    if not DDG_AVAILABLE:
        log.warning("ddgs not installed — skipping LinkedIn search")
        return None

    cleaned = clean_business_name_li(name)
    words = [w for w in cleaned.split() if len(w) >= 3]
    generic = {"auto", "cars", "motor", "motors", "used", "trade", "sale", "dealer"}
    non_generic = [w for w in words if w not in generic]
    if len(non_generic) < 1:
        log.info(f"    [linkedin] Skipping — name too generic: {name}")
        return None

    q1 = f'site:linkedin.com/company "{name}" Malaysia'
    q2 = f'site:linkedin.com/company "{domain}"' if domain else None

    urls1 = []
    urls2 = []

    # Query 1
    log.info(f"    [linkedin] DDG Q1: {q1}")
    try:
        with DDGS() as ddgs:
            results1 = list(ddgs.text(q1, max_results=DDG_MAX_RESULTS))
        time.sleep(SLEEP_DDG)
        for r in results1:
            u = r.get("href") or r.get("url", "")
            if u and "linkedin.com/company/" in u.lower():
                urls1.append(u.split("?")[0].rstrip("/"))
    except Exception as e:
        log.warning(f"DDG Q1 failed: {e}")

    # Query 2 (only if domain is available)
    if q2:
        log.info(f"    [linkedin] DDG Q2: {q2}")
        try:
            with DDGS() as ddgs:
                results2 = list(ddgs.text(q2, max_results=DDG_MAX_RESULTS))
            time.sleep(SLEEP_DDG)
            for r in results2:
                u = r.get("href") or r.get("url", "")
                if u and "linkedin.com/company/" in u.lower():
                    urls2.append(u.split("?")[0].rstrip("/"))
        except Exception as e:
            log.warning(f"DDG Q2 failed: {e}")

    # Consensus Check
    candidates = []
    if domain:
        # Check intersection
        intersect = [u for u in urls1 if u in urls2]
        if intersect:
            log.info(f"    [linkedin] Consensus found in both DDG queries: {intersect[0]}")
            candidates.append(intersect[0])
        else:
            if urls2:
                log.info(f"    [linkedin] Using domain search candidate: {urls2[0]}")
                candidates.append(urls2[0])
            if urls1:
                candidates.extend(urls1[:2])
    else:
        if urls1:
            candidates.extend(urls1[:2])

    candidates = list(dict.fromkeys(candidates))

    for url in candidates:
        if validate_linkedin_slug(url, name):
            if domain:
                if verify_linkedin_by_domain(url, domain, name):
                    return url
            else:
                try:
                    from rapidfuzz import fuzz
                    slug_match = LINKEDIN_COMPANY_RE.search(url)
                    if slug_match:
                        slug = slug_match.group(1).lower()
                        clean_name = clean_business_name_li(name)
                        clean_slug = slug.replace("-", " ").replace("_", " ").replace(".", " ")
                        ratio = fuzz.token_set_ratio(clean_name, clean_slug) / 100.0
                        if ratio >= 0.85:
                            log.info(f"    [linkedin] ✓ Accepted link without domain via high fuzzy match ({ratio:.2f}): {url}")
                            return url
                except Exception as e:
                    log.warning(f"Fuzzy matching failed: {e}")

    log.info(f"    [linkedin] ✗ No verified LinkedIn profile found")
    return None


# ─── MAIN ENRICHMENT FUNCTION ──────────────────────────────────────────────────

def enrich_email_linkedin(business: dict) -> dict:
    """
    Takes an already JinaWeb-enriched business dict.
    Adds 'emails', 'phones_extra', and 'linkedin' fields.
    Returns updated dict.
    """
    name    = business.get("name", "")
    address = business.get("address", "")
    city    = _extract_city(address)

    # Get website from JinaWeb links if available
    links       = business.get("links", {})
    website_entry = links.get("website", {})
    website     = (
        website_entry.get("url") if isinstance(website_entry, dict)
        else website_entry
    )

    log.info(f"\n  [EL] {name}")
    log.info(f"       Website : {website or 'none'}")

    # Email + phone extraction
    emails, phones_extra = extract_emails(website)

    fb_url = links.get("facebook", {}).get("url") if isinstance(links.get("facebook"), dict) else None
    if not emails and fb_url:
        log.info(f"    [email] Website crawl yielded no emails. Trying Facebook fallback: {fb_url}")
        fb_emails = extract_emails_from_facebook(fb_url, name)
        if fb_emails:
            emails.extend(fb_emails)
            log.info(f"    [email] Facebook fallback found emails: {fb_emails}")

    if not emails:
        log.info(f"    [email] Attempting direct search fallback for email...")
        search_emails = search_email_via_jina(name)
        if search_emails:
            emails.extend(search_emails)
            log.info(f"    [email] Direct search fallback found emails: {search_emails}")

    # LinkedIn discovery
    linkedin = find_linkedin(name, city, website)

    updated = business.copy()
    updated["emails"]       = emails
    updated["phones_extra"] = phones_extra
    updated["linkedin"]     = linkedin

    log.info(f"  [EL] emails={emails} | linkedin={linkedin}")
    return updated


def _extract_city(address: str) -> str:
    """Minimal city extractor — mirrors JinaWeb's version."""
    if not address:
        return "Malaysia"
    known = [
        "Kuala Lumpur", "Petaling Jaya", "Shah Alam", "Subang Jaya",
        "Klang", "Ampang", "Cheras", "Puchong", "Cyberjaya", "Putrajaya",
        "Johor Bahru", "Penang", "Georgetown", "Ipoh", "Kota Kinabalu",
        "Kuching", "Malacca", "Seremban", "Alor Setar", "Kuantan",
        "Bangsar", "Mont Kiara", "Damansara", "Kepong", "Setapak",
    ]
    for city in known:
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