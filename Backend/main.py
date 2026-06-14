"""
main.py — FindIt FastAPI backend
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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import job_manager
from search_logic import run_phase1
from jinaweb_logic import run_phase2
import supabase_client

# ─── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="FindIt API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path(__file__).parent.parent / "Phase_two"

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

def _load_from_cache(job: job_manager.Job) -> list | None:
    """Try Supabase first, then local JSON file. Returns list of dealers or None."""

    # ── 1. Supabase (primary database) ─────────────────────────────────────────
    if supabase_client.is_configured():
        try:
            job.log("=== Database Lookup: Checking Supabase... ===")
            sb_dealers = supabase_client.fetch_dealers(job.city, job.pincode, job.dealer_type)
            if sb_dealers:
                job.log(f"✓ Retrieved {len(sb_dealers)} enriched dealers from Supabase.")
                return sb_dealers
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
                return filtered
            else:
                job.log("⚠ Cache found, but no dealers matched the requested dealer type. Starting fresh scan...")
        except Exception as e:
            job.log(f"⚠ Local cache load failed: {e}. Starting fresh scan...")

    return None


def _run_job(job: job_manager.Job):
    """Full pipeline in a background thread: Phase 1 → Phase 2."""
    try:
        # ── Check Supabase / local cache first ──────────────────────────────────
        cached = _load_from_cache(job)
        if cached:
            with job.dealers_lock:
                job.dealers = list(cached)

            job.enrich_total = len(cached)
            job.enrich_done = len(cached)

            if job.small_target and job.target:
                cached.sort(key=lambda d: d.get("scoring", {}).get("score", 0), reverse=True)
                top = cached[: job.target]
                job.recommended_ids = [d.get("place_id") for d in top if d.get("place_id")]
            else:
                job.recommended_ids = [d.get("place_id") for d in cached if d.get("place_id")]

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


# ─── SERVE FRONTEND ────────────────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent.parent / "Frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")