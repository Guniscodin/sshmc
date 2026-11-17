"""Bounded in-memory state for background mod installation jobs."""

import time

JOBS: dict[str, dict] = {}
MAX_RETAINED_JOBS = 256
JOB_TTL_SECONDS = 60 * 60


def prune_jobs():
    """Drop old completed jobs and cap retained history without touching active work."""
    now = time.time()
    expired = [
        job_id for job_id, job in JOBS.items()
        if job.get("status") not in {"queued", "downloading"}
        and now - job.get("finished_at", job.get("created_at", now)) > JOB_TTL_SECONDS
    ]
    for job_id in expired:
        JOBS.pop(job_id, None)
    completed = sorted(
        ((job.get("finished_at", job.get("created_at", now)), job_id)
         for job_id, job in JOBS.items() if job.get("status") not in {"queued", "downloading"})
    )
    while len(JOBS) > MAX_RETAINED_JOBS and completed:
        _, job_id = completed.pop(0)
        JOBS.pop(job_id, None)
