# Automobile Dealer Intelligence

Automated pipeline that finds car dealers by city/pincode, enriches their profiles across multiple platforms, and scores them by digital presence.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, Uvicorn |
| Frontend | HTML, CSS, Vanilla JS |
| Database | Supabase (PostgreSQL) |
| Search | SerpAPI (Google Maps) |
| Web Discovery | Jina AI / DuckDuckGo fallback |
| Website Crawling | Firecrawl, Requests + BeautifulSoup |
| Email Discovery | Hunter.io (fallback) |
| LinkedIn | Piloterr, Jina AI & DuckDuckGo search |

---

## How It Works

```
User Input (city + pincode + dealer type)
        │
        ▼
Phase 1 — Google Maps Search (SerpAPI)
  └─ Finds all car dealers in the area
        │
        ▼
Phase 2 — Enrichment (per dealer)
  ├─ Official Website        ← Jina AI search + crawl
  ├─ Mudah listing           ← Jina AI search
  ├─ Carlist listing         ← Jina AI search
  ├─ Autocari listing        ← Jina AI search
  ├─ Facebook page           ← Jina AI search
  ├─ Instagram profile       ← Jina AI search
  ├─ TikTok profile          ← Jina AI search
  ├─ Email                   ← Hunter.io + website crawl
  └─ LinkedIn                ← Piloterr / Jina AI + DuckDuckGo search
        │
        ▼
Scoring (max 85 pts)
  Website(20) + Mudah(15) + Carlist(15) +
  Autocari(10) + Facebook(10) + Instagram(10) + TikTok(5)
        │
        ▼
Results saved to Supabase + local JSON cache
Top dealers returned to frontend
```

## Key Fixes & Data Resiliency (V1.0)

To resolve data quality issues and prevent blockages, the pipeline incorporates the following safeguards:
*   **Facebook Scrape Recovery**: Employs proxy-rotation (`X-No-Cache` header), request retry loops (up to 5 times), and content sanitization (stripping Markdown/href links to prevent false keyword matches inside redirects) to bypass login redirect blocks.
*   **Direct Search Email Fallback**: If website crawling is blocked, the engine searches search-engine snippets using Jina Search to pull emails directly from business listings.
*   **Social Profile Match Validation**: Enforces string distance check (`fuzz.token_set_ratio`), city slug validation, and phone suffix validation on all searched social handles (Facebook, Instagram, TikTok) to filter out personal pages or unrelated companies sharing names.
*   **Flexible Facebook URL Parsing**: Seamlessly resolves and parses complex profile structures, including standard custom names, `/p/` profile pages, `/people/` profile directories, and numeric ID links (`profile.php?id=...`).
*   **Singapore Boundary Filtering**: Filters out Singapore listings (based on `+65` / `02` phone prefixes or address strings) to prevent Singapore spillover in border cities like Johor Bahru.
*   **Supabase Data Syncing Guards**: Standardizes city values to title case, extracts actual 5-digit postcodes directly from addresses, and auto-generates Google Maps URLs from `place_id` if they are missing.
*   **Super CI Pipeline**: Validates code syntax and style rules automatically using `flake8` and `py_compile`.

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/morrplus/automobile-dealer-intelligence.git
cd automobile-dealer-intelligence
```

### 2. Install dependencies

```bash
pip install -r backend/requirements.txt
```

### 3. Configure API keys

```bash
cp .env.example .env
```

Open `.env` and fill in the following keys:

| Key | Where to get it |
|-----|----------------|
| `SERPAPI_KEY` | [serpapi.com](https://serpapi.com) — 100 free searches/month |
| `JINA_API_KEY` | [jina.ai](https://jina.ai) — web discovery and fallback scrape |
| `FIRECRAWL_API_KEY` | [firecrawl.dev](https://firecrawl.dev) — JS shell website crawling |
| `HUNTER_API_KEY` | [hunter.io](https://hunter.io) — email discovery fallback |
| `PILOTERR_API_KEY` | [piloterr.com](https://piloterr.com) — LinkedIn company search |
| `SUPABASE_URL` | Supabase project → Settings → API |
| `SUPABASE_KEY` | Supabase project → Settings → API → anon public |

> **Note:** Jina AI and Firecrawl are credit-based. DuckDuckGo is used as a free fallback when Jina credits run out.

### 4. Set up Supabase table

Run this SQL in your Supabase project → SQL Editor:

```sql
CREATE TABLE IF NOT EXISTS dealers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    place_id        TEXT UNIQUE,
    name            TEXT,
    city            TEXT,
    pincode         TEXT,
    dealer_type     TEXT,
    address         TEXT,
    phone           TEXT,
    website         TEXT,
    google_maps_url TEXT,
    email           TEXT,
    linkedin_url    TEXT,
    facebook_url    TEXT,
    instagram_url   TEXT,
    score           INTEGER,
    raw_data        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### 5. Run the server

```bash
cd backend
$env:PYTHONUTF8="1"; python -m uvicorn main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000)

---

## Caching Logic

1. **Supabase** — checked first on every search (fastest)
2. **Local JSON** — `Phase_two/dealers_enriched_<City>_<pincode>.json` — fallback
3. **Fresh scan** — runs if no cache found, results saved to both

---

## Project Structure

```
FindIt/
├── backend/
│   ├── main.py                  # FastAPI app + job orchestration
│   ├── search_logic.py          # Phase 1 — Google Maps via SerpAPI
│   ├── jinaweb_logic.py         # Phase 2 — Enrichment pipeline
│   ├── emaillinkedin_logic.py   # Email + LinkedIn extraction
│   ├── supabase_client.py       # Supabase read/write
│   ├── job_manager.py           # Background job tracking
│   ├── job_context.py           # Thread-local logging context
│   └── requirements.txt
├── Frontend/
│   └── index.html               # Single-page UI
├── Phase_two/                   # Local JSON cache (gitignored)
├── .env.example                 # API key template
└── README.md
```

---


