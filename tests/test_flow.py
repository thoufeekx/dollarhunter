import io
import re
from datetime import timedelta
from decimal import Decimal

from PIL import Image
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.config import get_settings
from app.models import (AccessToken, Basket, BasketLine, Customer, Job, LedgerEntry, Message, Order, Quote, Receipt,
                        StoredFile)
from app.services.outbox import enqueue_message, send_pending
from app.services.receipts import purge_expired_files
from app.storage import get_storage
from app.util import utcnow
from tests.test_core import A, B, Rec, drain, inbound

ADMIN = {"X-Admin-Token": "test-admin-token"}


def ready(sf, *phones):
    for ph in phones:
        with sf() as s:
            s.add(Customer(phone=ph, is_demo=True, onboarding_state="ready", postal_prefix="L3T")); s.commit()


def last_out(sf, phone):
    with sf() as s:
        c = s.scalar(select(Customer).where(Customer.phone == phone))
        return s.scalars(select(Message).where(Message.customer_id == c.id).order_by(Message.created_at.desc())).first().body


def say(sf, phone, text, mid):
    inbound(sf, phone, text, mid)
    drain(sf)
    return last_out(sf, phone)


def code_of(body):
    return re.search(r"YES (\d{6})", body).group(1)


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(buf, "JPEG")
    return buf.getvalue()


def test_quote_math_and_simulated_order(sf):
    ready(sf, A)
    say(sf, A, "2 avocados, 1 toothpaste, 1 paper towels, 1 milk", "q1")
    body = say(sf, A, "QUOTE", "q2")
    assert "You save $5.47 vs regular $20.46 (our fee $1.37" in body and "Total $16.36" in body
    assert "Not found: milk" in body and "http" not in body and "2 avocados $2.50" in body
    assert "DEMO: simulated order, no purchase or delivery booked" in say(sf, A, f"yes {code_of(body)}", "q3")
    with sf() as s:
        o = s.scalar(select(Order))
        assert o.mode == "simulated" and s.scalar(select(LedgerEntry)) is None  # ledger waits for completion
        assert s.scalar(select(Basket)).status == "ordered"
    assert "No open quote" in say(sf, A, f"YES {code_of(body)}", "q4")


def test_wrong_codes_lock_quote(sf):
    ready(sf, A)
    say(sf, A, "1 toothpaste", "w1")
    code = code_of(say(sf, A, "quote", "w2"))
    bad = "000000" if code != "000000" else "111111"
    for n in range(4):
        assert "does not match" in say(sf, A, f"YES {bad}", f"w3{n}")
    assert "cancelled" in say(sf, A, f"YES {bad}", "w4")
    assert "No open quote" in say(sf, A, f"YES {code}", "w5")
    with sf() as s:
        assert s.scalar(select(Order)) is None


def test_edit_after_quote_makes_it_stale(sf):
    ready(sf, A)
    say(sf, A, "1 toothpaste", "e1")
    code = code_of(say(sf, A, "quote", "e2"))
    say(sf, A, "2 avocados", "e3")
    assert "changed" in say(sf, A, f"YES {code}", "e4")
    with sf() as s:
        assert s.scalar(select(Order)) is None


def test_quotes_are_per_customer(sf):
    ready(sf, A, B)
    say(sf, A, "1 toothpaste", "p1")
    code = code_of(say(sf, A, "quote", "p2"))
    say(sf, B, "bananas", "p3")
    assert "No open quote" in say(sf, B, f"YES {code}", "p4")


def test_basket_command_and_edits_by_text(sf):
    ready(sf, A)
    say(sf, A, "toilet paper, dish soap", "b1")
    assert "toilet paper (qty?)" in say(sf, A, "BASKET", "b2")
    say(sf, A, "1 toilet paper, 2 dish soap", "b3")
    say(sf, A, "remove dish soap", "b4")
    body = say(sf, A, "QUOTE", "b5")
    assert "toilet paper $9.99" in body and "dish soap" not in body and "http" not in body


def test_postal_first_message(sf):
    say(sf, A, "L3T 1A1", "n1")
    with sf() as s:
        bodies = [m.body for m in s.scalars(select(Message))]
    assert len(bodies) == 1 and "What do you need" in bodies[0] and "http" not in bodies[0]
    with sf() as s:
        c = s.scalar(select(Customer))
        assert c.onboarding_state == "ready" and c.postal_prefix == "L3T"
        assert s.scalar(select(Basket)) is None


def _demo_receipt(client, phone, data):
    return client.post("/demo/receipt", headers=ADMIN, data={"phone": phone}, files={"file": ("r.jpg", data, "image/jpeg")})


def test_demo_receipt_mock_extraction_adds_items(sf, client):
    ready(sf, A)
    assert _demo_receipt(client, A, jpeg()).status_code == 200
    drain(sf)
    body = last_out(sf, A)
    assert body.startswith("MOCK") and "1) 2 avocados; 2) 1 toothpaste" in body and "ADD 1 3, or ADD ALL" in body
    with sf() as s:
        b = s.scalar(select(Basket))
        assert {l.status for l in s.scalars(select(BasketLine))} == {"suggested"}  # nothing added silently
        f = s.scalar(select(StoredFile))
        assert f.processing_status == "done" and f.storage_key.startswith("receipts/")
        assert s.scalar(select(Receipt)).is_mock
        assert not s.scalar(select(Job).where(Job.kind == "process_receipt", Job.status != "done"))
    assert get_storage().get(f.storage_key)
    _demo_receipt(client, A, jpeg())
    drain(sf)
    assert "already sent" in last_out(sf, A)


def test_receipt_rejects_non_images(sf, client):
    ready(sf, A)
    _demo_receipt(client, A, b"GIF89a-not-really")
    drain(sf)
    assert "JPG or PNG" in last_out(sf, A)
    _demo_receipt(client, A, b"%PDF-1.4 broken")
    drain(sf)
    assert "PDF" in last_out(sf, A)
    with sf() as s:
        assert s.scalar(select(StoredFile)) is None


def test_receipt_daily_limit(sf, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "daily_receipt_limit", 0)
    ready(sf, A)
    _demo_receipt(client, A, jpeg())
    drain(sf)
    assert "limit" in last_out(sf, A)


def test_purge_expired_files(sf, client):
    ready(sf, A)
    _demo_receipt(client, A, jpeg())
    drain(sf)
    with sf() as s:
        f = s.scalar(select(StoredFile)); f.retention_expires_at = utcnow() - timedelta(days=1); key = f.storage_key
        s.commit()
    assert purge_expired_files(sf) == 1
    with sf() as s:
        assert s.scalar(select(StoredFile)).deleted_at is not None
    try:
        get_storage().get(key)
        assert False, "file should be gone"
    except FileNotFoundError:
        pass


def test_demo_refuses_real_customers(sf, client):
    with sf() as s:
        s.add(Customer(phone=A, is_demo=False)); s.commit()
    assert client.post("/demo/inbound", headers=ADMIN, json={"phone": A, "text": "hi"}).status_code == 409
    assert client.get("/demo/conversation", headers=ADMIN, params={"phone": A}).status_code == 409
    assert client.get("/demo/conversation", params={"phone": A}).status_code == 404


def test_outbox_gsm_cap_and_demo_routing(sf, monkeypatch):
    with sf() as s:
        real, demo = Customer(phone=A), Customer(phone=B, is_demo=True)
        s.add_all([real, demo]); s.flush()
        m = enqueue_message(s, real, "It’s “ready” — ok", "reply", "g1")
        assert m.body == "It's \"ready\" - ok" and m.segments == 1
        m2 = enqueue_message(s, real, "NO NAME\u00ae EGGS; Fra\u00eeches \u00e9pinards \u2014 caf\u00e9", "reply", "g1b")
        assert m2.body == "NO NAME EGGS; Fraiches \u00e9pinards - caf\u00e9" and m2.segments == 1
        enqueue_message(s, demo, "demo text", "reply", "g2")
        s.commit()
    real_p, demo_p = Rec(), Rec()
    demo_p.name = "demo"
    send_pending(sf, real_p, demo_provider=demo_p)
    assert {t for t, _ in real_p.sent} == {A} and [t for t, _ in demo_p.sent] == [B]
    monkeypatch.setattr(get_settings(), "daily_sms_limit", 1)
    with sf() as s:
        real = s.scalar(select(Customer).where(Customer.phone == A))
        assert enqueue_message(s, real, "over the cap", "reply", "g3") is None
        assert enqueue_message(s, real, "stop ack", "stop_ack", "g4", allow_when_opted_out=True) is not None


def test_twilio_webhook_passes_media(client, sf, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "twilio_auth_token", "tok"); monkeypatch.setattr(s, "twilio_account_sid", "ACx")
    monkeypatch.setattr(s, "public_base_url", "https://dh.example.com")
    params = {"AccountSid": "ACx", "MessageSid": "MM1", "From": A, "Body": "", "NumMedia": "1",
              "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/ACx/Messages/MM1/Media/ME1",
              "MediaContentType0": "image/jpeg"}
    sig = RequestValidator("tok").compute_signature("https://dh.example.com/sms/inbound", params)
    assert client.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": sig}).status_code == 200
    with sf() as db:
        j = db.scalar(select(Job))
        assert j.payload["media"] == [{"url": params["MediaUrl0"], "type": "image/jpeg"}]


def test_prune_old_rows_keeps_recent_and_pending(sf):
    from app.models import Event
    from app.services.retention import prune_old_rows
    old = utcnow() - timedelta(days=200)
    with sf() as s:
        c = Customer(phone=A); s.add(c); s.flush()
        s.add_all([Job(kind="x", dedupe_key="old-done", status="done", finished_at=old),
                   Job(kind="x", dedupe_key="old-queued", status="queued"),
                   Job(kind="x", dedupe_key="new-done", status="done", finished_at=utcnow()),
                   Message(customer_id=c.id, body="old", dedupe_key="m-old", status="accepted", created_at=old),
                   Message(customer_id=c.id, body="old queued", dedupe_key="m-q", status="queued", created_at=old),
                   Message(customer_id=c.id, body="new", dedupe_key="m-new", status="accepted"),
                   Event(kind="x", created_at=old),
                   AccessToken(customer_id=c.id, purpose="basket", token_hash="h", expires_at=old)])
        s.commit()
    assert prune_old_rows(sf) == {"jobs": 1, "messages": 1, "events": 1, "tokens": 1}
    with sf() as s:
        assert sorted(j.dedupe_key for j in s.scalars(select(Job))) == ["new-done", "old-queued"]
        assert sorted(m.dedupe_key for m in s.scalars(select(Message))) == ["m-new", "m-q"]


def test_backup_keeps_newest_seven(monkeypatch, capsys):
    import sys
    from app import backup
    st = get_storage()
    for i in range(9):
        st.put(f"backups/dollarhunter-2020010{i}T000000Z.sql.gz", b"x")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"y" * 200)))
    backup.main()
    left = sorted(p.name for p in (st.root / "backups").glob("*.sql.gz"))
    assert len(left) == 7 and left[-1].startswith("dollarhunter-20") and "20200100" not in left[0]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    try:
        backup.main()
        assert False, "empty backup must be refused"
    except SystemExit:
        pass
