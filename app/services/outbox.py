import logging
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models import Customer, Message
from app.services.sms_text import segments, to_gsm
from app.util import mask_phone, utcnow

log = logging.getLogger("dh.outbox")


def enqueue_message(s, customer: Customer, body: str, kind: str, dedupe_key: str, allow_when_opted_out=False):
    if customer.opted_out and not allow_when_opted_out:
        return None
    if not allow_when_opted_out and not customer.is_demo:
        since = utcnow() - timedelta(days=1)
        sent_today = s.scalar(select(func.count(Message.id)).where(Message.customer_id == customer.id,
                                                                   Message.created_at > since))
        if sent_today >= get_settings().daily_sms_limit:
            log.warning("daily SMS limit reached for %s", mask_phone(customer.phone))
            return None
    body = to_gsm(body)
    _, n = segments(body)
    try:
        with s.begin_nested():
            m = Message(customer_id=customer.id, body=body, segments=n, kind=kind, dedupe_key=dedupe_key,
                        allow_when_opted_out=allow_when_opted_out)
            s.add(m)
            s.flush()
        return m
    except IntegrityError:
        return None


def suppress_queued(s, customer_id):
    s.execute(update(Message).where(Message.customer_id == customer_id, Message.status == "queued",
                                    Message.allow_when_opted_out.is_(False)).values(status="suppressed"))


def send_pending(session_factory, provider, limit=10, demo_provider=None) -> int:
    """Send queued messages. demo_provider, when given, handles demo customers so they never cost real SMS."""
    sent = 0
    for _ in range(limit):
        with session_factory() as s:
            m = s.execute(select(Message).where(Message.direction == "out", Message.status == "queued")
                          .order_by(Message.created_at).limit(1).with_for_update(skip_locked=True)).scalar_one_or_none()
            if not m:
                return sent
            c = s.get(Customer, m.customer_id)
            if c.opted_out and not m.allow_when_opted_out:
                m.status = "suppressed"
                s.commit()
                continue
            p = demo_provider if (c.is_demo and demo_provider) else provider
            m.status, m.provider = "sending", p.name
            to, body, key, mid = c.phone, m.body, m.dedupe_key, m.id
            s.commit()
        status, sid, err = None, None, None
        try:
            r = p.send(to, body, key)
            status, sid = r.status, r.sid
        except Exception as e:
            code = getattr(e, "status", None)
            status = "failed" if isinstance(code, int) and 400 <= code < 500 else "unknown"
            err = type(e).__name__
            log.warning("send %s for %s: %s", status, mask_phone(to), err)
        with session_factory() as s:
            m = s.get(Message, mid)
            m.status, m.provider_sid, m.error, m.sent_at = status, sid, err, utcnow()
            s.commit()
        sent += 1
    return sent
