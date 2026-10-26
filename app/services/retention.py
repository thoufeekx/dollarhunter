import logging
from datetime import timedelta

from sqlalchemy import delete

from app.models import AccessToken, Event, Job, Message
from app.util import utcnow

log = logging.getLogger("dh.retention")
JOB_DAYS, MESSAGE_DAYS, EVENT_DAYS, TOKEN_DAYS = 14, 90, 90, 1


def prune_old_rows(session_factory) -> dict:
    """Delete housekeeping rows past their retention window, oldest first. Business records are kept."""
    now = utcnow()
    with session_factory() as s:
        n = {
            "jobs": s.execute(delete(Job).where(Job.status.in_(["done", "failed"]),
                                                Job.finished_at < now - timedelta(days=JOB_DAYS))).rowcount,
            "messages": s.execute(delete(Message).where(Message.created_at < now - timedelta(days=MESSAGE_DAYS),
                                                        Message.status.notin_(["queued", "sending"]))).rowcount,
            "events": s.execute(delete(Event).where(Event.created_at < now - timedelta(days=EVENT_DAYS))).rowcount,
            "tokens": s.execute(delete(AccessToken).where(AccessToken.expires_at < now - timedelta(days=TOKEN_DAYS))
                                ).rowcount,
        }
        s.commit()
    if any(n.values()):
        log.info("pruned old rows: %s", n)
    return n
