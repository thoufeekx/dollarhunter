import pytest
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.config import Settings, get_settings
from app.models import Basket, BasketLine, Customer, Input, Job, Message
from app.providers.base import SendResult
from app.providers.fixtures import RuleListParser
from app.services.inbound import record_inbound
from app.services.jobs import claim_job
from app.services.outbox import send_pending
from app.services.sms_text import segments
from app.util import utcnow
from app.worker import run_one

A, B = "+14165550101", "+14165550102"


class Rec:
    name = "console"

    def __init__(self, exc=None):
        self.sent, self.exc = [], exc

    def send(self, to, body, key):
        if self.exc:
            raise self.exc
        self.sent.append((to, body))
        return SendResult(status="simulated", provider="console")


def inbound(sf, phone, text, mid):
    with sf() as s:
        _, created = record_inbound(s, "demo", mid, phone, text, demo=True)
        s.commit()
    return created


def drain(sf):
    while run_one(sf):
        pass


def test_segments():
    assert segments("a" * 160) == ("GSM-7", 1) and segments("a" * 161) == ("GSM-7", 2)
    assert segments("a" * 70 + "é\u2019")[0] == "UCS-2"
    assert segments("\u2019" * 71) == ("UCS-2", 2)


def test_parser_examples():
    p = RuleListParser().parse("Bananas, two avocados and toothpaste. No meat this week.")
    names = {i.sku: i.quantity for i in p.items}
    assert names == {"bananas": None, "avocados": 2, "toothpaste": None}
    assert [(i.type, i.text) for i in p.instructions] == [("exclude", "meat")]
    q = RuleListParser().parse("fruit")
    assert q.items[0].question


def test_startup_guards():
    with pytest.raises(RuntimeError):
        Settings(DH_PAYMENTS_ENABLED=True, _env_file=None).validate_startup()
    with pytest.raises(RuntimeError):
        Settings(DH_REAL_ORDERS_ENABLED=True, _env_file=None).validate_startup()
    with pytest.raises(RuntimeError):
        Settings(DH_ENV="production", DH_ADMIN_TOKEN="", _env_file=None).validate_startup()
    assert Settings(DH_SMS_PROVIDER="twilio", _env_file=None).effective_sms_provider == "console"


def test_duplicate_inbound_is_idempotent(sf):
    assert inbound(sf, A, "bananas", "m1") and not inbound(sf, A, "bananas", "m1")
    with sf() as s:
        assert len(s.scalars(select(Input)).all()) == 1 and len(s.scalars(select(Job)).all()) == 1


def test_two_users_separate_baskets_and_messages(sf):
    for ph in (A, B):
        with sf() as s:
            s.add(Customer(phone=ph, is_demo=True, onboarding_state="ready")); s.commit()
    inbound(sf, A, "two avocados", "a1")
    inbound(sf, B, "3 toothpaste, skip nothing", "b1")
    drain(sf)
    with sf() as s:
        ca = s.scalar(select(Customer).where(Customer.phone == A)); cb = s.scalar(select(Customer).where(Customer.phone == B))
        la = s.scalars(select(BasketLine).where(BasketLine.customer_id == ca.id)).all()
        lb = s.scalars(select(BasketLine).where(BasketLine.customer_id == cb.id)).all()
        assert [l.name for l in la] == ["avocados"] and [l.name for l in lb] == ["toothpaste"]
        ma = s.scalars(select(Message).where(Message.customer_id == ca.id)).all()
        assert len(ma) == 1 and "avocados" in ma[0].body and "toothpaste" not in ma[0].body
    rec = Rec()
    send_pending(sf, rec)
    assert {t for t, _ in rec.sent} == {A, B}


def test_onboarding_and_no_meat_is_basket_only(sf):
    inbound(sf, A, "bananas, no meat this week", "o1")
    drain(sf)
    inbound(sf, A, "L3T 1A1", "o2")
    drain(sf)
    with sf() as s:
        c = s.scalar(select(Customer).where(Customer.phone == A))
        assert c.onboarding_state == "ready" and c.postal_prefix == "L3T"
        assert c.preferences == {"substitutions": "ask"}
        b = s.scalar(select(Basket).where(Basket.customer_id == c.id))
        assert b.instructions == [{"type": "exclude", "text": "meat"}]


def test_stop_suppresses_queued_and_start_does_not_order(sf):
    inbound(sf, A, "bananas", "s1")
    drain(sf)
    inbound(sf, A, "STOP", "s2")
    drain(sf)
    rec = Rec()
    send_pending(sf, rec)
    assert len(rec.sent) == 1 and "unsubscribed" in rec.sent[0][1]
    inbound(sf, A, "bananas", "s3")
    drain(sf)
    with sf() as s:
        assert s.scalar(select(Message).where(Message.dedupe_key.like("in:%")).where(Message.status == "queued")) is None
    inbound(sf, A, "START", "s4")
    drain(sf)
    with sf() as s:
        c = s.scalar(select(Customer).where(Customer.phone == A))
        assert not c.opted_out and c.sms_consent_state == "granted"


def test_lease_recovery_and_retry_limit(sf):
    inbound(sf, A, "bananas", "l1")
    jid = claim_job(sf)
    assert jid and claim_job(sf) is None
    with sf() as s:
        j = s.get(Job, jid)
        j.lease_expires_at = utcnow().replace(year=2000); s.commit()
    assert claim_job(sf) == jid
    with sf() as s:
        assert s.get(Job, jid).attempts == 2


def test_failing_job_retries_then_fails(sf, monkeypatch):
    import app.worker as w
    monkeypatch.setitem(w.HANDLERS, "process_inbound", lambda s, p: (_ for _ in ()).throw(RuntimeError("boom")))
    inbound(sf, A, "bananas", "f1")
    for _ in range(3):
        assert run_one(sf)
        with sf() as s:
            j = s.scalar(select(Job))
            if j.status == "queued":
                j.run_after = utcnow().replace(year=2000); s.commit()
    with sf() as s:
        j = s.scalar(select(Job))
        assert j.status == "failed" and j.attempts == 3


def test_unknown_send_is_not_retried(sf):
    inbound(sf, A, "bananas", "u1")
    drain(sf)
    send_pending(sf, Rec(exc=TimeoutError()))
    with sf() as s:
        assert s.scalar(select(Message)).status == "unknown"
    rec = Rec()
    send_pending(sf, rec)
    assert rec.sent == []


def test_twilio_webhook_signature_and_dedupe(client, sf, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "twilio_auth_token", "tok"); monkeypatch.setattr(s, "twilio_account_sid", "ACx")
    monkeypatch.setattr(s, "public_base_url", "https://dh.example.com")
    params = {"AccountSid": "ACx", "MessageSid": "SM1", "From": A, "Body": "bananas", "NumMedia": "0"}
    sig = RequestValidator("tok").compute_signature("https://dh.example.com/sms/inbound", params)
    assert client.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    for _ in range(2):
        r = client.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200 and "<Response/>" in r.text
    with sf() as db:
        assert len(db.scalars(select(Input)).all()) == 1 and len(db.scalars(select(Job)).all()) == 1


def test_admin_protection(client):
    assert client.post("/demo/inbound", json={"phone": A, "text": "x"}).status_code == 404
    r = client.post("/demo/inbound", json={"phone": A, "text": "x"}, headers={"X-Admin-Token": "test-admin-token"})
    assert r.status_code == 200 and client.get("/healthz").json()["real_orders"] is False
