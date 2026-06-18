"""
main.py — MORR AutoScrape FastAPI backend
Handles both the dealer intelligence scraper engine AND the auth/campaign layer.
"""

import threading
import json
import sys
from pathlib import Path

# Force UTF-8 output on Windows to prevent charmap encode errors
# for Unicode characters (e.g. checkmarks) written to stdout/stderr
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException, Request, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import List, Optional
import uuid
import os
import requests

import job_manager
from search_logic import run_phase1
from jinaweb_logic import run_phase2
import supabase_client

# ─── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="MORR AutoScrape API")

# Session middleware must be added BEFORE CORSMiddleware
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "morr-autoscrape-secret-2024")
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR    = Path(__file__).parent.parent / "Phase_two"
UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

# Jinja2 templates (Backend/templates/)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ─── SCHEMAS ───────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    city:        str
    pincode:     str
    dealer_type: str   # "used" | "new" | "both"
    target:      int


class RespondRequest(BaseModel):
    job_id: str
    expand: bool


# ─── BACKGROUND JOB ────────────────────────────────────────────────────────────

def _load_from_cache(job: job_manager.Job) -> tuple[list | None, str | None]:
    """Try Supabase first, then local JSON file. Returns (dealers_list, source)."""

    # ── 1. Supabase (primary database) ─────────────────────────────────────────
    if supabase_client.is_configured():
        try:
            job.log("=== Database Lookup: Checking Supabase... ===")
            sb_dealers = supabase_client.fetch_dealers(job.city, job.pincode, job.dealer_type)
            if sb_dealers:
                job.log(f"✓ Retrieved {len(sb_dealers)} enriched dealers from Supabase.")
                return sb_dealers, "supabase"
            else:
                job.log("No records in Supabase for this search. Running fresh scan...")
        except Exception as e:
            job.log(f"⚠ Supabase lookup failed: {e}. Trying local cache...")

    # ── 2. Local JSON file (fallback) ───────────────────────────────────────────
    cache_filename = f"dealers_enriched_{job.city.replace(' ', '_')}_{job.pincode}.json"
    cache_path = DATA_DIR / cache_filename
    if not cache_path.exists():
        for child in DATA_DIR.glob("*.json"):
            if child.name.lower() == cache_filename.lower():
                cache_path = child
                break

    if cache_path.exists():
        job.log(f"=== Database Lookup: Found local cache — {cache_path.name} ===")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached_dealers = json.load(f)

            filtered = []
            for d in cached_dealers:
                d_type = d.get("type", "").lower()
                if job.dealer_type == "used" and "used" not in d_type and "recond" not in d_type:
                    continue
                if job.dealer_type == "new" and "new" not in d_type:
                    continue
                filtered.append(d)

            if filtered:
                job.log(f"✓ Retrieved {len(filtered)} enriched dealers from local cache.")
                return filtered, "local_json"
            else:
                job.log("⚠ Cache found, but no dealers matched the requested dealer type. Starting fresh scan...")
        except Exception as e:
            job.log(f"⚠ Local cache load failed: {e}. Starting fresh scan...")

    return None, None


def _run_job(job: job_manager.Job):
    """Full pipeline in a background thread: Phase 1 → Phase 2."""
    try:
        # ── Check Supabase / local cache first ──────────────────────────────────
        cached, source = _load_from_cache(job)
        if cached:
            with job.dealers_lock:
                job.dealers = list(cached)

            # Show progress against what user asked for, not the raw cache size
            display_total        = job.target if (job.small_target and job.target) else len(cached)
            job.enrich_total     = display_total
            job.enrich_done      = display_total

            if job.small_target and job.target:
                cached.sort(key=lambda d: d.get("scoring", {}).get("score", 0), reverse=True)
                top = cached[: job.target]
                job.recommended_ids = [d.get("place_id") for d in top if d.get("place_id")]
            else:
                job.recommended_ids = [d.get("place_id") for d in cached if d.get("place_id")]

            # Sync local JSON cache to Supabase if not already there
            if source == "local_json" and supabase_client.is_configured():
                try:
                    job.log("Syncing local cache to Supabase...")
                    result = supabase_client.upsert_dealers(
                        cached, job.city, job.pincode, job.dealer_type
                    )
                    job.log(f"✓ Supabase sync: {result['inserted']} dealers saved.")
                    if result["errors"]:
                        for err in result["errors"][:3]:
                            job.log(f"  ⚠ Sync error: {err['dealer']} — {err['error']}")
                except Exception as e:
                    job.log(f"⚠ Supabase sync failed: {e}")

            job.phase = "done"
            job.log("=== All done (Loaded from Cache) ✓ ===")
            return

        # ── Phase 1 ─────────────────────────────────────────────────────────────
        job.phase = "searching"
        job.log("=== Phase 1: Searching Google Maps ===")
        dealers = run_phase1(job)

        if job.phase == "error":
            return

        if not dealers:
            job.phase = "error"
            job.error = "No dealers found."
            return

        job.log(f"\n✓ Phase 1 complete — {len(dealers)} dealers found")
        job.log("=== Phase 2: Enriching dealer profiles ===")

        # ── Phase 2 ─────────────────────────────────────────────────────────────
        job.phase = "enriching"
        run_phase2(job, dealers, DATA_DIR)

        # ── Save to Supabase ─────────────────────────────────────────────────────
        if supabase_client.is_configured():
            try:
                with job.dealers_lock:
                    enriched = list(job.dealers)
                if enriched:
                    job.log(f"Saving {len(enriched)} dealers to Supabase...")
                    result = supabase_client.upsert_dealers(
                        enriched, job.city, job.pincode, job.dealer_type
                    )
                    job.log(f"✓ Supabase: {result['inserted']} dealers saved.")
                    if result["errors"]:
                        for err in result["errors"][:3]:   # show at most 3 errors
                            job.log(f"  ⚠ Save error: {err['dealer']} — {err['error']}")
            except Exception as e:
                job.log(f"⚠ Supabase save failed (results still returned): {e}")

        job.phase = "done"
        job.log("=== All done ✓ ===")

    except Exception as e:
        job.phase = "error"
        job.error = str(e)
        job.log(f"✗ Fatal error: {e}")


# ─── ENDPOINTS ─────────────────────────────────────────────────────────────────

@app.post("/search/start")
def start_search(req: StartRequest):
    """Start a new search+enrichment job. Returns job_id immediately."""
    job = job_manager.create_job(req.city, req.pincode, req.dealer_type, req.target)
    t   = threading.Thread(target=_run_job, args=(job,), daemon=True)
    t.start()
    return {"job_id": job.id}


@app.get("/search/status")
def get_status(job_id: str):
    """Poll job status, log, dealers, and progress."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_status()


@app.post("/search/respond")
def respond_expand(req: RespondRequest):
    """Answer the radius-expansion prompt (yes/no)."""
    job = job_manager.get_job(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.phase != "awaiting_expand":
        raise HTTPException(status_code=400, detail="No expansion prompt active")
    job.expand_answer = req.expand
    job.expand_event.set()
    return {"ok": True}


# ─── AUTH & CAMPAIGN ROUTES ───────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, tab: str = "login"):
    """Serve the login/signup page."""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
        "tab": tab
    })


@app.post("/login", response_class=HTMLResponse)
async def login_handler(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    action: str = Form("login")
):
    """Handle login and signup form submissions."""
    email = email.strip().lower()

    if not email or not password:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Please fill in all fields.",
            "tab": action
        })

    if action == "signup":
        if len(password) < 6:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Password must be at least 6 characters.",
                "tab": "signup"
            })
        result = supabase_client.create_user(email, password)
        if not result["success"]:
            error_msg = result.get("error", "Sign-up failed.")
            # Friendly message for duplicate email
            if "duplicate" in error_msg.lower() or "unique" in error_msg.lower():
                error_msg = "Account already exists. Please log in."
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": error_msg,
                "tab": "login"
            })
        request.session["user_email"] = email
        return RedirectResponse(url="/dashboard", status_code=303)

    else:  # login
        result = supabase_client.verify_user(email, password)
        if not result["success"]:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": result.get("error", "Login failed."),
                "tab": "login",
                "google_hint": result.get("hint") == "google_oauth"
            })
        request.session["user_email"] = email
        return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/auth/login/google")
async def google_login(request: Request):
    """Redirect to Google's OAuth 2.0 consent screen."""
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Google OAuth is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in your .env file.",
            "tab": "login"
        })

    base_url = str(request.base_url).rstrip('/')
    if request.headers.get("x-forwarded-proto") == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI") or (base_url + "/auth/callback/google")

    google_auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=openid%20email%20profile"
        f"&prompt=select_account"
    )
    return RedirectResponse(url=google_auth_url)


@app.get("/auth/callback/google")
async def google_callback(request: Request, code: str = None, error: str = None):
    """Handle the OAuth callback from Google."""
    if error:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": f"Google login error: {error}",
            "tab": "login"
        })
    if not code:
        return RedirectResponse(url="/login")

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Google OAuth credentials are missing on the server.",
            "tab": "login"
        })

    base_url = str(request.base_url).rstrip('/')
    if request.headers.get("x-forwarded-proto") == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI") or (base_url + "/auth/callback/google")

    # Exchange authorization code for access token
    token_url = "https://oauth2.googleapis.com/token"
    token_payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }

    try:
        token_res = requests.post(token_url, data=token_payload, timeout=10)
        token_res.raise_for_status()
        token_data = token_res.json()
        access_token = token_data.get("access_token")

        # Fetch user info using the access token
        userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"
        userinfo_res = requests.get(userinfo_url, params={"access_token": access_token}, timeout=10)
        userinfo_res.raise_for_status()
        userinfo = userinfo_res.json()

        email = userinfo.get("email")
        if not email:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Could not retrieve email address from your Google profile.",
                "tab": "login"
            })

        email = email.strip().lower()

        # Verify or register user in our custom Supabase user table
        try:
            if supabase_client.is_configured():
                client = supabase_client.get_client()
                res = client.table("users").select("*").eq("email", email).execute()
                if not res.data:
                    # Auto-register new OAuth user with a random hashed password
                    random_pass = str(uuid.uuid4())
                    create_res = supabase_client.create_user(
                        email, random_pass, oauth_provider="google"
                    )
                    if not create_res["success"]:
                        return templates.TemplateResponse("login.html", {
                            "request": request,
                            "error": f"Failed to register Google account in our database: {create_res.get('error')}",
                            "tab": "login"
                        })
            else:
                return templates.TemplateResponse("login.html", {
                    "request": request,
                    "error": "Database is not configured.",
                    "tab": "login"
                })
        except Exception as db_err:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": f"Database connection error: {db_err}",
                "tab": "login"
            })

        # Save session & redirect to dashboard
        request.session["user_email"] = email
        return RedirectResponse(url="/dashboard", status_code=303)

    except Exception as e:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": f"Failed to authenticate with Google: {e}",
            "tab": "login"
        })



@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Serve the dealer campaign dashboard (requires login)."""
    user_email = request.session.get("user_email")
    if not user_email:
        return RedirectResponse(url="/login", status_code=303)
    campaigns = supabase_client.get_campaigns(user_email)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "email": user_email,
        "campaigns": campaigns
    })


@app.post("/submit")
async def submit_campaign(
    request: Request,
    dealership_name: str = Form(""),
    budget: float = Form(0),
    days: int = Form(0),
    weeks: int = Form(0),
    months: int = Form(0),
    media: Optional[List[UploadFile]] = File(None)
):
    """Save a new campaign to Supabase. Saves uploaded media files locally."""
    user_email = request.session.get("user_email")
    if not user_email:
        return {"error": "Not logged in"}

    # Save uploaded files
    uploaded_filenames = []
    if media:
        for f in media:
            if f and f.filename:
                ext = Path(f.filename).suffix.lower()
                safe_name = str(uuid.uuid4()) + ext
                dest = UPLOADS_DIR / safe_name
                content = await f.read()
                dest.write_bytes(content)
                uploaded_filenames.append(safe_name)

    duration = {"days": days, "weeks": weeks, "months": months}
    result = supabase_client.create_campaign(
        user_email=user_email,
        dealership_name=dealership_name.strip(),
        budget_myr=budget,
        duration=duration,
        files=uploaded_filenames
    )

    if result["success"]:
        return {"success": True, "message": "Campaign saved!", "files": len(uploaded_filenames)}
    return {"success": False, "error": result.get("error", "Failed to save.")}


# ─── SERVE UPLOADS ────────────────────────────────────────────────────────────

if UPLOADS_DIR.exists():
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


# ─── LANDING PAGE ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    """Always serve the marketing landing page. Login/signup buttons handle auth flow."""
    logged_in = bool(request.session.get("user_email"))
    return templates.TemplateResponse("landing.html", {
        "request": request,
        "logged_in": logged_in
    })


# ─── SERVE SCRAPER FRONTEND (mounted at /scraper) ──────────────────────────────

FRONTEND_DIR = Path(__file__).parent.parent / "Frontend"
if FRONTEND_DIR.exists():
    app.mount("/scraper", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")