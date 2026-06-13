"""
job_context.py — Thread-local job context so enrichment modules can log
without threading job_id through every function call.
"""

import threading

_local = threading.local()


def set_job(job):
    _local.job = job


def get_job():
    return getattr(_local, "job", None)


def log_info(msg: str):
    job = get_job()
    if job:
        job.log(msg)


def log_warning(msg: str):
    job = get_job()
    if job:
        job.log(f"⚠ {msg}")
