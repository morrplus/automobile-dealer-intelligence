"""
main.py — FindIt FastAPI backend
"""

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import job_manager
from search_logic import run_phase1
from jinaweb_logic import run_phase2

# ─── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="FindIt API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path(__file__).parent.parent / "Phase_Two"

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

def _run_job(job: job_manager.Job):
    """Full pipeline in a background thread: Phase 1 → Phase 2."""
    try:
        # Phase 1
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

        # Phase 2 (runs automatically — no prompt)
        job.phase = "enriching"
        run_phase2(job, dealers, DATA_DIR)

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

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")