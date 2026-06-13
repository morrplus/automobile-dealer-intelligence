"""
job_manager.py — In-memory job store and state machine
"""

import uuid
import threading
from typing import Optional


# ─── JOB STATE ─────────────────────────────────────────────────────────────────

class Job:
    def __init__(self, city: str, pincode: str, dealer_type: str, target: int):
        self.id           = str(uuid.uuid4())
        self.city         = city
        self.pincode      = pincode
        self.dealer_type  = dealer_type
        self.target       = target
        self.small_target = target <= 7

        # Phase tracking
        self.phase        = "starting"   # starting | searching | awaiting_expand | enriching | done | error
        self.error        = None

        # Log buffer
        self.log_lines    = []
        self.log_lock     = threading.Lock()

        # Phase 1 results
        self.dealers      = []           # list of dicts from Phase 1
        self.dealers_lock = threading.Lock()

        # Expansion prompt state
        self.expand_event     = threading.Event()
        self.expand_answer    = None     # True / False
        self.expand_prompt    = None     # dict describing the prompt or None

        # Phase 2 progress
        self.enrich_total     = 0
        self.enrich_done      = 0
        self.enrich_current   = None    # name of business being enriched now
        self.recommended_ids  = []      # place_ids of top-N (or all) after ranking

    def log(self, msg: str):
        with self.log_lock:
            self.log_lines.append(msg)

    def to_status(self) -> dict:
        with self.dealers_lock:
            dealers_copy = list(self.dealers)
        return {
            "job_id":           self.id,
            "phase":            self.phase,
            "error":            self.error,
            "log":              list(self.log_lines[-200:]),  # last 200 lines
            "expand_prompt":    self.expand_prompt,
            "dealers":          dealers_copy,
            "enrich_total":     self.enrich_total,
            "enrich_done":      self.enrich_done,
            "enrich_current":   self.enrich_current,
            "recommended_ids":  self.recommended_ids,
        }


# ─── GLOBAL JOB STORE ──────────────────────────────────────────────────────────

_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def create_job(city: str, pincode: str, dealer_type: str, target: int) -> Job:
    job = Job(city, pincode, dealer_type, target)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)
