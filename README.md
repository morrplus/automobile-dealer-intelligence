# MORR AutoScrape Intelligence — V1.0

> **AI-powered dealer intelligence & ad campaign platform for the Malaysian used-car market.**

---

## 🚀 What This Project Does

MORR AutoScrape is a full-stack intelligence platform that:

1. **Discovers** automobile dealers across Malaysia via Google Maps (Phase 1)
2. **Enriches** each dealer profile with social media links, marketplace profiles, emails, and a digital-presence score (Phase 2)
3. **Serves** all this through a polished dealer portal with authentication, campaign management, and market intelligence views

---

## 🗂️ Project Structure

```
automobile-dealer-intelligence/
│
├── Backend/                        # FastAPI backend (main application)
│   ├── main.py                     # All routes: auth, scraper API, campaigns, OAuth
│   ├── search_logic.py             # Phase 1 — Google Maps dealer discovery
│   ├── jinaweb_logic.py            # Phase 2 — Social + marketplace enrichment
│   ├── emaillinkedin_logic.py      # Email, phone, LinkedIn extraction + cross-social
│   ├── supabase_client.py          # Supabase DB helpers (users, dealers, campaigns)
│   ├── job_manager.py              # Background job tracking
│   ├── job_context.py              # Thread-local logging context
│   ├── requirements.txt            # Python dependencies
│   └── templates/
│       ├── landing.html            # Public marketing landing page
│       ├── login.html              # Auth page (email+password + Google OAuth)
│       └── dashboard.html          # Dealer portal (overview, intel, campaigns)
│
├── Frontend/                       # Standalone scraper UI (embedded in dashboard)
│   └── index.html
│
├── Phase_One/                      # Legacy standalone scraper scripts
│   ├── SearchMap.py
│   └── converting.py
│
├── Phase_two/                      # Legacy standalone enrichment scripts
│   ├── JinaWeb.py
│   └── EmailLinkedin.py
│
├── .env.example                    # Template for required API keys
├── .gitignore
└── README.md
```

---

## ⚙️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12 · FastAPI · Uvicorn |
| Auth | Session middleware · SHA-256 hashing · Google OAuth 2.0 |
| Database | Supabase (PostgreSQL) |
| Scraping | Jina AI Search · Jina Reader · DuckDuckGo fallback |
| Enrichment | Firecrawl · Hunter.io · Piloterr · Apify · BeautifulSoup |
| AI/LLM | Google Gemini · Groq · OpenRouter |
| Frontend | Vanilla HTML/CSS/JS · Fraunces + Inter fonts |

---

## 🔑 Required Environment Variables

Copy `.env.example` to `.env` and fill in your keys:

```env
# Search
SERPAPI_KEY=
JINA_API_KEY=
TAVILY_API_KEY=

# Enrichment
FIRECRAWL_API_KEY=
HUNTER_API_KEY=
PROXYCURL_API_KEY=
APIFY_API_TOKEN=
PILOTERR_API_KEY=

# AI
GEMINI_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=

# Database
SUPABASE_URL=
SUPABASE_KEY=

# Google OAuth
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
# GOOGLE_REDIRECT_URI=http://localhost:8000/auth/callback/google
```

---

## 🗄️ Supabase Tables

Run these once in your Supabase SQL editor:

```sql
-- Dealer intelligence store
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

-- User accounts (custom auth)
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT DEFAULT 'dealer',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Ad campaigns
CREATE TABLE IF NOT EXISTS campaigns (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email       TEXT NOT NULL,
    dealership_name  TEXT,
    budget_myr       FLOAT,
    duration         JSONB,
    files            JSONB,
    submitted_at     TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 🏃 Running Locally

```bash
cd Backend
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Then open: [http://localhost:8000](http://localhost:8000)

---

## ✅ Features Implemented (as of V1.0)

### Authentication
- Email + password signup/login with SHA-256 hashing
- **Google OAuth 2.0** — click "Sign in with Google"
- Google-account users blocked from email+password login with a guided error
- Session-based auth — all dashboard data scoped to logged-in user

### Market Intelligence (Phase 2 Enrichment)
- Google Maps dealer discovery (city + postcode)
- Social media enrichment: **Facebook · Instagram · TikTok**
- Marketplace enrichment: **Mudah.my · Carlist.my · Autocari.com**
- **Cross-social discovery** — reads Facebook page to find Instagram/TikTok links and vice versa
- Website crawl for emails, phone numbers, and social links in footers/headers
- Carlist.my dealer profile preference (`/dealer/` URLs prioritised over generic listings)
- Digital presence **scoring system** (0–85 points across 7 platforms)
- Supabase caching + local JSON fallback with resume support
- Real-time job progress streaming

### Dealer Portal (Dashboard)
- Premium dark-sidebar layout with Fraunces + Inter typography
- **Overview** — campaign count, budget, dealer stats
- **Market Intelligence** — embedded live scraper UI
- **Ad Campaigns** — set budget (MYR), duration (days/weeks/months), upload media
- Campaign data persisted to Supabase per user

### Landing Page
- Animated aurora gradient background
- Feature showcase with scroll animations
- CTA buttons routing to login/dashboard

---

## 🗺️ Roadmap

| Date | Milestone |
|---|---|
| ✅ Jun 17 | Login · Budget · Media upload · Dashboard UI live |
| 🔲 Jun 18 | Swarm agent — distribute ads to FB · IG · TikTok · Mudah · Carlist · iCar |
| 🔲 Jun 19 | A/B variants per ad · CTR tracking · Auto-pause underperformer |
| 🔲 Jun 20 | Lead capture · Webhook · Downstream agent triggering |
| 🔲 Jun 21 | Full demo — Atul approval to proceed to agent layer |

---

## 👥 Team

Built by **Sriya** & **Priyanshu** for **MORR** — June 2026 internship sprint.
