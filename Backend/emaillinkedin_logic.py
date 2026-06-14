"""
emaillinkedin_logic.py — Email + LinkedIn enrichment, logging via job_context
"""

import re
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from difflib import SequenceMatcher

import requests
import threading
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import job_context

try:
    from ddgs import DDGS
    DDG_AVAILABLE = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        DDG_AVAILABLE = True
    except ImportError:
        DDG_AVAILABLE = False

load_dotenv(Path(__file__).parent.parent / ".env")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
HUNTER_API_KEY    = os.getenv("HUNTER_API_KEY")
PILOTERR_API_KEY  = os.getenv("PILOTERR_API_KEY")
JINA_API_KEY      = os.getenv("JINA_API_KEY")
JINA_TIMEOUT      = 25

_CRAWL_CACHE = {}
_CRAWL_CACHE_LOCK = threading.Lock()

REQUEST_TIMEOUT    = 8
FIRECRAWL_TIMEOUT  = 30
JS_SHELL_THRESHOLD = 2000
MAX_PAGES_TO_CRAWL = 4
DDG_MAX_RESULTS    = 8
SLEEP_DDG          = 1.5

FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"
HUNTER_URL    = "https://api.hunter.io/v2/domain-search"

CONTACT_PATHS = [
    "/contact", "/contact-us", "/contact_us", "/contactus",
    "/about", "/about-us", "/about_us",
    "/reach-us", "/hubungi-kami", "/team",
]

EMAIL_BLACKLIST = [
    r"example\.", r"@sentry\.", r"@google\.", r"noreply", r"no-reply",
    r"support@", r"privacy@", r"legal@", r"admin@", r"webmaster@",
    r"info@wixpress", r"@wix\.", r"@wordpress\.", r"@gravatar\.",
]

SOCIAL_DOMAINS = [
    "facebook.com", "instagram.com", "tiktok.com", "mudah.my",
    "carlist.my", "linkedin.com", "youtube.com", "twitter.com",
]

FOREIGN_LI_SUFFIXES = (
    "-au", "-uk", "-us", "-sg", "-id", "-ph",
    "-th", "-vn", "-cn", "-jp", "-kr", "-in",
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)
MY_PHONE_RE = re.compile(
    r"(?:\+?60|0)[\s\-]?(?:1[0-9][\s\-]?\d{3,4}[\s\-]?\d{4}|[3-9][\s\-]?\d{2,4}[\s\-]?\d{4})",
    re.I,
)
LINKEDIN_COMPANY_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9\-_%.]+)/?", re.I
)


def fetch_page(url: str) -> str | None:
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
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code < 400:
            html = r.text
    except Exception:
        pass

    if html and len(html.strip()) >= JS_SHELL_THRESHOLD:
        return html

    if FIRECRAWL_API_KEY:
        try:
            job_context.log_info(f"    [Firecrawl] JS shell — fetching: {url}")
            resp = requests.post(
                FIRECRAWL_URL,
                headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}", "Content-Type": "application/json"},
                json={"url": url, "formats": ["html"], "onlyMainContent": False},
                timeout=FIRECRAWL_TIMEOUT,
            )
            if resp.status_code == 200:
                fc_html = resp.json().get("data", {}).get("html")
                if fc_html and len(fc_html.strip()) > (len(html.strip()) if html else 0):
                    return fc_html
        except Exception as e:
            job_context.log_warning(f"Firecrawl failed: {e}")

    return html


def is_clean_email(email: str) -> bool:
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
    soup = BeautifulSoup(html, "lxml")

    placeholder_emails: set[str] = set()
    for tag in soup.find_all(True, {"placeholder": True}):
        ph = tag.get("placeholder", "")
        if "@" in ph:
            m = EMAIL_RE.search(ph)
            if m:
                placeholder_emails.add(m.group(0).lower())

    mailto_emails: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            if addr and "@" in addr and addr not in placeholder_emails:
                mailto_emails.append(addr)

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

    plain    = soup.get_text(separator=" ")
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

    all_emails = list(dict.fromkeys(mailto_emails + regex_emails))
    all_phones = list(dict.fromkeys(tel_phones + regex_phones))
    clean_emails = [e.strip() for e in all_emails if is_clean_email(e.strip())]
    return clean_emails, all_phones


def clean_facebook_url(url: str) -> str:
    if not url:
        return ""
    url = url.split("?")[0].rstrip("/")
    if "profile.php" in url:
        return url
    
    match = re.search(r"https?://(?:[a-z0-9\-]+\.)?facebook\.com/([^/]+)", url, re.I)
    if match:
        username = match.group(1).lower()
        if username in ("groups", "events", "photo.php", "permalink.php", "share", "watch", "reel", "sharer", "people"):
            if username == "people":
                parts = urlparse(url).path.strip("/").split("/")
                if len(parts) >= 3:
                    return f"https://www.facebook.com/people/{parts[1]}/{parts[2]}/"
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
        
    job_context.log_info(f"    [crawl] Crawling website for socials + contacts: {website_url}")
    
    all_emails = []
    all_phones = []
    all_socials = {}
    visited = set()
    
    def crawl(url: str):
        if url in visited or len(visited) >= MAX_PAGES_TO_CRAWL:
            return
        visited.add(url)
        job_context.log_info(f"    [crawl] Page: {url}")
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
                job_context.log_info(f"    [email] Hunter.io fallback for: {domain}")
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
                        job_context.log_info(f"    [email] Hunter.io found: {hunter_emails}")
                        all_emails.extend(hunter_emails)
        except Exception as e:
            job_context.log_warning(f"Hunter.io failed: {e}")
            
    res = {
        "emails": all_emails,
        "phones": all_phones,
        "socials": all_socials
    }
    
    with _CRAWL_CACHE_LOCK:
        _CRAWL_CACHE[website_url] = res
        
    return res

def extract_emails_from_facebook(fb_url: str) -> list[str]:
    if not fb_url:
        return []
    try:
        jina_url = f"https://r.jina.ai/{fb_url}"
        headers = {"Accept": "application/json"}
        if JINA_API_KEY:
            headers["Authorization"] = f"Bearer {JINA_API_KEY}"
        resp = requests.get(jina_url, headers=headers, timeout=JINA_TIMEOUT)
        if resp.status_code == 200:
            content = resp.text
            found = EMAIL_RE.findall(content)
            clean_found = [e.lower().strip() for e in found if is_clean_email(e.strip())]
            return list(dict.fromkeys(clean_found))
    except Exception as e:
        job_context.log_warning(f"Failed to scrape Facebook page via Jina: {e}")
    return []

def lookup_linkedin_by_domain(domain: str) -> str | None:
    if not PILOTERR_API_KEY or not domain:
        return None
    try:
        resp = requests.get(
            "https://piloterr.com/api/v2/linkedin/company/url",
            params={"query": domain},
            headers={"x-api-key": PILOTERR_API_KEY},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            url = data.get("data")
            if url:
                return url.split("?")[0].rstrip("/")
    except Exception as e:
        job_context.log_warning(f"Piloterr lookup failed: {e}")
    return None

def verify_linkedin_by_domain(linkedin_url: str, website_domain: str) -> bool:
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
                job_context.log_info(f"    [linkedin] Jina validation SUCCESS: {clean_domain} found on LinkedIn page")
                return True
            else:
                job_context.log_info(f"    [linkedin] Jina validation FAILED: {clean_domain} not found on LinkedIn page")
                return False
    except Exception as e:
        job_context.log_warning(f"Jina validation failed for LinkedIn: {e}")
    return False

def extract_emails(website_url: str | None) -> tuple[list[str], list[str]]:
    res = crawl_website_for_all(website_url)
    return res["emails"], res["phones"]


def clean_business_name_li(name: str) -> str:
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
    try:
        m = LINKEDIN_COMPANY_RE.search(url)
        if not m:
            return False
        slug = m.group(1).lower()
        if any(slug.endswith(s) for s in FOREIGN_LI_SUFFIXES):
            job_context.log_info(f"    [linkedin] Rejected — foreign suffix: {slug}")
            return False
        parsed    = urlparse(url)
        subdomain = parsed.netloc.lower().split(".")[0]
        if subdomain not in ("www", "my", "linkedin"):
            job_context.log_info(f"    [linkedin] Rejected — foreign subdomain: {subdomain}")
            return False
        cleaned    = clean_business_name_li(business_name)
        slug_words = slug.replace("-", " ").split()
        name_words = [w for w in cleaned.split() if len(w) >= 4]
        ratio      = SequenceMatcher(None, cleaned, slug.replace("-", " ")).ratio()
        word_hits  = [w for w in name_words if w in slug_words]
        min_hits   = 2 if len(name_words) <= 2 else 1
        if ratio >= 0.55 and len(word_hits) >= min_hits:
            return True
        job_context.log_info(f"    [linkedin] Rejected slug (ratio={ratio:.2f}, hits={word_hits}): {slug}")
        return False
    except Exception:
        return True


def find_linkedin(name: str, city: str, website_url: str | None = None) -> str | None:
    # Layer 1: Already handled by extracting from website crawl
    if website_url:
        cached = crawl_website_for_all(website_url)
        li_from_web = cached.get("socials", {}).get("linkedin")
        if li_from_web:
            job_context.log_info(f"    [linkedin] Layer 1: Found direct link on website: {li_from_web}")
            return li_from_web

    # Resolve domain if website is available
    domain = ""
    if website_url:
        try:
            domain = urlparse(website_url).netloc.lower().replace("www.", "")
        except Exception:
            pass

    # Layer 2: Piloterr domain-to-LinkedIn API
    if domain:
        job_context.log_info(f"    [linkedin] Layer 2: Querying Piloterr for domain: {domain}")
        piloterr_li = lookup_linkedin_by_domain(domain)
        if piloterr_li:
            job_context.log_info(f"    [linkedin] Piloterr returned: {piloterr_li}")
            if verify_linkedin_by_domain(piloterr_li, domain):
                job_context.log_info(f"    [linkedin] ✓ Verified Piloterr link via Jina: {piloterr_li}")
                return piloterr_li

    # Layer 3: Dual DuckDuckGo queries consensus
    if not DDG_AVAILABLE:
        job_context.log_warning("ddgs not installed — skipping LinkedIn search")
        return None

    cleaned = clean_business_name_li(name)
    words = [w for w in cleaned.split() if len(w) >= 3]
    generic = {"auto", "cars", "motor", "motors", "used", "trade", "sale", "dealer"}
    non_generic = [w for w in words if w not in generic]
    if len(non_generic) < 1:
        job_context.log_info(f"    [linkedin] Skipping — name too generic: {name}")
        return None

    q1 = f'site:linkedin.com/company "{name}" Malaysia'
    q2 = f'site:linkedin.com/company "{domain}"' if domain else None

    urls1 = []
    urls2 = []

    # Query 1
    job_context.log_info(f"    [linkedin] DDG Q1: {q1}")
    try:
        with DDGS() as ddgs:
            results1 = list(ddgs.text(q1, max_results=DDG_MAX_RESULTS))
        time.sleep(SLEEP_DDG)
        for r in results1:
            u = r.get("href") or r.get("url", "")
            if u and "linkedin.com/company/" in u.lower():
                urls1.append(u.split("?")[0].rstrip("/"))
    except Exception as e:
        job_context.log_warning(f"DDG Q1 failed: {e}")

    # Query 2 (only if domain is available)
    if q2:
        job_context.log_info(f"    [linkedin] DDG Q2: {q2}")
        try:
            with DDGS() as ddgs:
                results2 = list(ddgs.text(q2, max_results=DDG_MAX_RESULTS))
            time.sleep(SLEEP_DDG)
            for r in results2:
                u = r.get("href") or r.get("url", "")
                if u and "linkedin.com/company/" in u.lower():
                    urls2.append(u.split("?")[0].rstrip("/"))
        except Exception as e:
            job_context.log_warning(f"DDG Q2 failed: {e}")

    # Consensus Check
    candidates = []
    if domain:
        # Check intersection
        intersect = [u for u in urls1 if u in urls2]
        if intersect:
            job_context.log_info(f"    [linkedin] Consensus found in both DDG queries: {intersect[0]}")
            candidates.append(intersect[0])
        else:
            if urls2:
                job_context.log_info(f"    [linkedin] Using domain search candidate: {urls2[0]}")
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
                if verify_linkedin_by_domain(url, domain):
                    return url
            else:
                from rapidfuzz import fuzz
                slug_match = LINKEDIN_COMPANY_RE.search(url)
                if slug_match:
                    slug = slug_match.group(1).lower()
                    clean_name = clean_business_name_li(name)
                    clean_slug = slug.replace("-", " ").replace("_", " ").replace(".", " ")
                    ratio = fuzz.token_set_ratio(clean_name, clean_slug) / 100.0
                    if ratio >= 0.85:
                        job_context.log_info(f"    [linkedin] ✓ Accepted link without domain via high fuzzy match ({ratio:.2f}): {url}")
                        return url

    job_context.log_info(f"    [linkedin] ✗ No verified LinkedIn profile found")
    return None


def _extract_city(address: str) -> str:
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


def enrich_email_linkedin(business: dict) -> dict:
    name    = business.get("name", "")
    address = business.get("address", "")
    city    = _extract_city(address)

    links         = business.get("links", {})
    website_entry = links.get("website", {})
    website       = (
        website_entry.get("url") if isinstance(website_entry, dict)
        else website_entry
    )

    job_context.log_info(f"  [EL] {name}")
    job_context.log_info(f"       Website: {website or 'none'}")

    emails = business.get("emails")
    phones_extra = business.get("phones_extra")
    linkedin = business.get("linkedin")

    if emails is None or phones_extra is None:
        emails, phones_extra = extract_emails(website)
    else:
        emails = list(emails)
        phones_extra = list(phones_extra)

    fb_url = links.get("facebook", {}).get("url") if isinstance(links.get("facebook"), dict) else None
    if not emails and fb_url:
        job_context.log_info(f"    [email] Website crawl yielded no emails. Trying Facebook fallback: {fb_url}")
        fb_emails = extract_emails_from_facebook(fb_url)
        if fb_emails:
            emails.extend(fb_emails)
            job_context.log_info(f"    [email] Facebook fallback found emails: {fb_emails}")

    if not linkedin:
        linkedin = find_linkedin(name, city, website)

    updated = business.copy()
    updated["emails"]       = emails
    updated["phones_extra"] = phones_extra
    updated["linkedin"]     = linkedin

    job_context.log_info(f"  [EL] emails={emails} | linkedin={linkedin}")
    return updated
