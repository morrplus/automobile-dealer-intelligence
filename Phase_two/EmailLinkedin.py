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

# ─── CONFIG ────────────────────────────────────────────────────────────────────

REQUEST_TIMEOUT    = 8
FIRECRAWL_TIMEOUT  = 30
JS_SHELL_THRESHOLD = 2000   # chars — below this = JS-only page, try Firecrawl
MAX_PAGES_TO_CRAWL = 4      # homepage + up to 3 contact/about subpages
DDG_MAX_RESULTS    = 8
SLEEP_DDG          = 1.5    # seconds between DDG calls

FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"
HUNTER_URL    = "https://api.hunter.io/v2/domain-search"

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
    """
    Crawls dealer website homepage + contact subpages.
    Returns (emails, extra_phones).
    """
    if not website_url:
        return [], []

    # Skip social/marketplace URLs — no emails there
    if any(s in website_url.lower() for s in SOCIAL_DOMAINS):
        log.info("    [email] Website is social/marketplace — skipping crawl")
        return [], []

    log.info(f"\n  [email] Crawling: {website_url}")

    all_emails: list[str] = []
    all_phones: list[str] = []
    visited:    set[str]  = set()

    def crawl(url: str):
        if url in visited or len(visited) >= MAX_PAGES_TO_CRAWL:
            return
        visited.add(url)
        log.info(f"    [email] Page: {url}")
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
        if emails:
            log.info(f"    [email] Found emails: {emails}")
        if phones:
            log.info(f"    [email] Found phones: {phones}")

    crawl(website_url)
    base = website_url.rstrip("/")
    for path in CONTACT_PATHS:
        if len(visited) >= MAX_PAGES_TO_CRAWL:
            break
        crawl(base + path)
        time.sleep(0.5)

    # Hunter.io fallback — only when website crawl found 0 emails
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
                        e["value"] for e in
                        resp.json().get("data", {}).get("emails", [])
                        if e.get("value") and is_clean_email(e["value"])
                    ]
                    if hunter_emails:
                        log.info(f"    [email] Hunter.io found: {hunter_emails}")
                        all_emails.extend(hunter_emails)
        except Exception as e:
            log.warning(f"    [email] Hunter.io failed: {e}")

    log.info(f"  [email] Final — emails: {all_emails} | phones: {all_phones}")
    return all_emails, all_phones


# ─── LINKEDIN DISCOVERY ────────────────────────────────────────────────────────

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


def find_linkedin(name: str, city: str) -> str | None:
    """
    Searches DuckDuckGo for the LinkedIn company page.
    Uses site:linkedin.com/company to get the URL from search index
    without ever hitting LinkedIn directly.
    Returns linkedin.com/company/... URL or None.
    """
    if not DDG_AVAILABLE:
        log.warning("    [linkedin] ddgs not installed — skipping LinkedIn search")
        return None

    # Skip truly generic single-word names — too many false positives
    cleaned   = clean_business_name_li(name)
    words     = [w for w in cleaned.split() if len(w) >= 3]
    generic   = {"auto", "cars", "motor", "motors", "used", "trade", "sale", "dealer"}
    non_generic = [w for w in words if w not in generic]
    if len(non_generic) < 1:
        log.info(f"    [linkedin] Skipping — name too generic: {name}")
        return None

    queries = [
        f'site:linkedin.com/company "{name}" Malaysia',
        f'site:linkedin.com/company "{name}" car dealer',
    ]

    for query in queries:
        log.info(f"    [linkedin] DDG: {query}")
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=DDG_MAX_RESULTS))
            time.sleep(SLEEP_DDG)
        except Exception as e:
            log.warning(f"    [linkedin] DDG failed: {e}")
            continue

        for r in results:
            url = r.get("href") or r.get("url", "")
            if not url or "linkedin.com/company/" not in url.lower():
                continue
            clean_url = url.split("?")[0].rstrip("/")
            if validate_linkedin_slug(clean_url, name):
                log.info(f"    [linkedin] ✓ Found: {clean_url}")
                return clean_url
            
    log.info(f"    [linkedin] ✗ Not found")
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

    # LinkedIn discovery
    linkedin = find_linkedin(name, city)

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