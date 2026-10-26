"""Acceptance cases taken from the Dollar Hunter build brief."""
import io
import re
from decimal import Decimal

from sqlalchemy import select

from app.models import BasketLine, LedgerEntry, Order, Quote
from app.services import quote as qt
from app.services.receipts import redact
from tests.test_core import A
from tests.test_flow import ADMIN, code_of, last_out, ready, say


def test_one_cottonelle_pack_matches_brief(sf):
    ready(sf, A)
    say(sf, A, "1 toilet paper", "c1")
    body = say(sf, A, "QUOTE", "c2")
    assert "toilet paper $9.99" in body and "You save $4.00 vs regular $13.99 (our fee $1.00" in body
    assert "Total $10.99" in body
    with sf() as s:
        q = s.scalar(select(Quote))
        assert (q.gross_savings, q.fee, q.net_savings, q.total) == (Decimal("4.00"), Decimal("1.00"), Decimal("3.00"),
                                                                     Decimal("10.99"))


def test_three_packs_over_budget_are_blocked_until_cap_raised(sf):
    ready(sf, A)
    say(sf, A, "3 toilet paper", "t1")
    body = say(sf, A, "QUOTE", "t2")
    assert "save $12.00" in body and "fee $3.00" in body and "Total $32.97" in body
    assert "Over your $25.00 budget" in body and "YES" not in body
    assert "over your budget" in say(sf, A, "YES 123456", "t3")
    assert "Budget set to $40.00" in say(sf, A, "budget 40", "t4")
    body = say(sf, A, "QUOTE", "t5")
    assert "DEMO: simulated order" in say(sf, A, f"YES {code_of(body)}", "t6")


def test_missing_quantity_blocks_quote(sf):
    ready(sf, A)
    say(sf, A, "bananas, 2 avocados", "m1")
    body = say(sf, A, "QUOTE", "m2")
    assert "How many of each: bananas" in body and "avocados" not in body.split("?")[0]
    with sf() as s:
        assert s.scalar(select(Quote)) is None
    say(sf, A, "2 bananas", "m3")
    assert "Total" in say(sf, A, "QUOTE", "m4")


def test_receipt_suggestions_are_picked_by_text(sf, client):
    from tests.test_core import drain
    from tests.test_flow import _demo_receipt, jpeg
    ready(sf, A)
    _demo_receipt(client, A, jpeg())
    drain(sf)
    assert "Which do you need this time" in say(sf, A, "QUOTE", "r1")
    say(sf, A, "toothpaste", "r2")  # naming a suggested item accepts it
    with sf() as s:
        st = {l.name: l.status for l in s.scalars(select(BasketLine))}
    assert st == {"toothpaste": "proposed", "avocados": "suggested"}
    assert "2 avocados" in say(sf, A, "ADD 1", "r3")
    with sf() as s:
        assert {l.status for l in s.scalars(select(BasketLine))} == {"proposed"}
    assert "no receipt items waiting" in say(sf, A, "ADD ALL", "r4")


def test_voice_note_mms_is_transcribed_into_basket(sf, client):
    from tests.test_core import drain
    ready(sf, A)
    r = client.post("/demo/receipt", headers=ADMIN, data={"phone": A}, files={"file": ("v.m4a", b"x" * 200, "audio/mp4")})
    assert r.status_code == 200
    drain(sf)
    body = last_out(sf, A)
    assert body.startswith("MOCK (voice AI off) Heard:") and "2 avocados" in body and "toothpaste" in body
    client.post("/demo/receipt", headers=ADMIN, data={"phone": A}, files={"file": ("v.amr", b"x" * 200, "audio/amr")})
    drain(sf)
    assert "cannot play that voice note format" in last_out(sf, A)


def test_text_pdf_sends_only_text_and_encrypted_pdf_is_refused(sf, client, monkeypatch):
    from pypdf import PdfWriter
    import app.services.receipts as rc
    from app.providers.ai import MockExtractor
    seen = {}

    class Spy(MockExtractor):
        def extract(self, data, mime, text=None):
            seen.update(mime=mime, text=text)
            return super().extract(data, mime, text)

    monkeypatch.setattr(rc, "get_extractor", lambda: Spy())
    monkeypatch.setattr(rc, "read_pdf", lambda data: "NO FRILLS\nCOLGATE TP 120ML 3.49\nAVOCADO 2 @1.99 3.98\nTOTAL 7.47")
    ready(sf, A)
    from tests.test_flow import _demo_receipt
    from tests.test_core import drain
    _demo_receipt(client, A, b"%PDF-1.4 text receipt")
    drain(sf)
    assert seen["mime"] == "application/pdf" and "COLGATE" in seen["text"]
    monkeypatch.undo()
    w = PdfWriter(); w.add_blank_page(100, 100); w.encrypt("secret")
    buf = io.BytesIO(); w.write(buf)
    _demo_receipt(client, A, buf.getvalue())
    drain(sf)
    assert "not password protected" in last_out(sf, A)


def test_completion_then_cancellation_reverses_ledger(sf, client):
    ready(sf, A)
    say(sf, A, "1 toilet paper", "l1")
    say(sf, A, f"YES {code_of(say(sf, A, 'QUOTE', 'l2'))}", "l3")
    with sf() as s:
        oid = s.scalar(select(Order)).id
    assert client.post(f"/demo/orders/{oid}/complete", headers=ADMIN).json()["ledger_entry"] == "completion"
    assert client.post(f"/demo/orders/{oid}/complete", headers=ADMIN).json()["ledger_entry"] is None  # idempotent
    assert client.post(f"/demo/orders/{oid}/cancel", headers=ADMIN).json()["ledger_entry"] == "reversal"
    assert client.post(f"/demo/orders/{oid}/complete").status_code == 404  # admin only
    with sf() as s:
        es = s.scalars(select(LedgerEntry)).all()
        assert sorted(e.entry_type for e in es) == ["completion", "reversal"] and sum(e.net for e in es) == 0


def test_redaction():
    assert redact("VISA ************1234 4111 1111 1111 1111") == "VISA ************[redacted]"
    assert redact("PC OPTIMUM 603207 123456789") == "PC OPTIMUM [redacted]"
    assert redact("COLGATE TP 120ML 3.49") == "COLGATE TP 120ML 3.49"
    assert redact("HASS AVOCADO 2 @1.99       3.98") == "HASS AVOCADO 2 @1.99       3.98"
    assert redact("10/24/2026 14:32 CALL 416-555-0101") == "10/24/2026 14:32 CALL [redacted]"
