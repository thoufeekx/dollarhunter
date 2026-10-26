import logging
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.models import Job
from app.util import utcnow

log = logging.getLogger("dh.jobs")
LEASE_SECONDS = 60


def enqueue_job(s, kind: str, dedupe_key: str, customer_id=None, payload=None):
    try:
        with s.begin_nested():
            j = Job(kind=kind, dedupe_key=dedupe_key, customer_id=customer_id, payload=payload or {})
            s.add(j)
            s.flush()
        return j
    except IntegrityError:
        return None


def claim_job(session_factory):
    now = utcnow()
    with session_factory() as s:
        j = s.execute(select(Job).where(
            or_(Job.status == "queued", (Job.status == "running") & (Job.lease_expires_at < now)),
            Job.run_after <= now).order_by(Job.run_after).limit(1).with_for_update(skip_locked=True)).scalar_one_or_none()
        if not j:
            return None
        if j.attempts >= j.max_attempts:
            j.status, j.finished_at, j.last_error = "failed", now, j.last_error or "lease expired after max attempts"
            s.commit()
            return None
        j.status, j.attempts = "running", j.attempts + 1
        j.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        s.commit()
        return j.id


def finish_job(session_factory, job_id, error: str | None = None):
    with session_factory() as s:
        j = s.get(Job, job_id)
        now = utcnow()
        if error is None:
            j.status, j.finished_at, j.lease_expires_at = "done", now, None
        elif j.attempts >= j.max_attempts:
            j.status, j.finished_at, j.last_error, j.lease_expires_at = "failed", now, error[:300], None
        else:
            j.status, j.last_error, j.lease_expires_at = "queued", error[:300], None
            j.run_after = now + timedelta(seconds=5 * 4 ** (j.attempts - 1))
        s.commit()


def retry_failed(s, job):
    job.status, job.attempts, job.run_after, job.finished_at = "queued", 0, utcnow(), None
