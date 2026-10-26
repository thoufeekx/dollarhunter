import hashlib
import io
import logging
import re
import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
from PIL import Image
from sqlalchemy import func, select

from app.catalog import match_staple
from app.config import get_settings
from app.models import Customer, Event, Input, Receipt, ReceiptLine, StoredFile
from app.providers.ai import AUDIO_FORMATS, get_extractor, get_transcriber
from app.providers.base import ParsedItem, ParsedList
from app.providers.fixtures import RuleListParser
from app.services import basket as bk
from app.services import sms_text as T
from app.services.outbox import enqueue_message
from app.storage import get_storage
from app.util import mask_phone, utcnow

log = logging.getLogger("dh.receipts")
TWILIO_MEDIA = "https://api.twilio.com/"
FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp"),
           "GIF": ("image/gif", "gif")}
MIN_CONFIDENCE = Decimal("0.5")
MAX_PDF_PAGES = 10
MIN_PDF_TEXT = 40  # fewer extracted characters than this means a scanned PDF
NUMBER_RUN = re.compile(r"\d+(?:[ -]\d+)*")  # digit groups joined by single spaces or dashes


def redact(text: str | None) -> str | None:
    """Mask card, loyalty and phone numbers (9+ digits in one run); prices and dates stay readable."""
    if not text:
        return text
    return NUMBER_RUN.sub(lambda m: "[redacted]" if sum(c.isdigit() for c in m.group()) >= 9 else m.group(), text)


class Unsupported(Exception):
    pass


def fetch_media(url: str, limit: int) -> bytes:
    """Download Twilio MMS media (auth required), refusing anything else or anything too large."""
    if not url.startswith(TWILIO_MEDIA):
        raise Unsupported("media host not allowed")
    s = get_settings()
    with httpx.stream("GET", url, auth=(s.twilio_account_sid, s.twilio_auth_token), follow_redirects=True,
                      timeout=30) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > limit:
                raise Unsupported("file too large")
    return bytes(buf)


def delete_twilio_media(url: str):
    if not url.startswith(TWILIO_MEDIA):
        return
    s = get_settings()
    try:
        httpx.delete(url, auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=10)
    except httpx.HTTPError:
        log.info("could not delete Twilio media copy")


def read_pdf(data: bytes) -> str:
    """Validate a PDF and return its text layer ('' for scanned PDFs)."""
    from pypdf import PdfReader
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            raise Unsupported("encrypted pdf")
        if len(r.pages) > MAX_PDF_PAGES:
            raise Unsupported("too many pages")
        return "\n".join((pg.extract_text() or "") for pg in r.pages).strip()
    except Unsupported:
        raise
    except Exception:
        raise Unsupported("malformed pdf")


def sniff(data: bytes) -> tuple[str, str]:
    if data[:5] == b"%PDF-":
        return "application/pdf", "pdf"
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
    except Exception:
        raise Unsupported("not an image")
    if im.format not in FORMATS:
        raise Unsupported(f"unsupported image {im.format}")
    return FORMATS[im.format]


def handle_process_receipt(s, payload: dict):
    inp = s.get(Input, payload["input_id"])
    c = s.get(Customer, inp.customer_id)
    cfg = get_settings()
    out = lambda body: enqueue_message(s, c, body, "receipt", f"rc:{inp.id}:{payload['n']}")
    if c.opted_out:
        return
    since = utcnow() - timedelta(days=1)
    if s.scalar(select(func.count(Receipt.id)).where(Receipt.customer_id == c.id, Receipt.created_at > since)) \
            >= cfg.daily_receipt_limit:
        out(T.RECEIPT_LIMIT)
        return
    storage = get_storage()
    if is_audio(payload.get("type", "")):
        return _voice_note(s, c, inp, payload, storage, out)
    try:
        if payload.get("demo_key"):
            data = storage.get(payload["demo_key"])
        else:
            data = fetch_media(payload["url"], cfg.max_upload_bytes)
        mime, ext = sniff(data)
        pdf_text = read_pdf(data) if mime == "application/pdf" else None
    except Unsupported as e:
        log.info("receipt rejected for %s: %s", mask_phone(c.phone), e)
        out(T.RECEIPT_BAD_FILE)
        return
    sha = hashlib.sha256(data).hexdigest()
    if s.scalar(select(StoredFile.id).where(StoredFile.customer_id == c.id, StoredFile.sha256 == sha,
                                            StoredFile.processing_status == "done")):
        out(T.RECEIPT_DUPLICATE)
        return
    fid = uuid.uuid4()
    key = f"receipts/{c.id}/{fid}.{ext}"
    f = StoredFile(id=fid, customer_id=c.id, input_id=inp.id, storage_key=key, detected_mime=mime, byte_size=len(data),
                   sha256=sha, retention_expires_at=utcnow() + timedelta(days=cfg.file_retention_days))
    s.add(f)
    s.flush()
    storage.put(key, data)
    try:
        ro = get_extractor().extract(data, mime, pdf_text if pdf_text and len(pdf_text) >= MIN_PDF_TEXT else None)
    except Exception as e:  # fail fast: retrying a paid AI call rarely helps a bad photo
        log.warning("receipt extraction failed for %s: %s", mask_phone(c.phone), type(e).__name__)
        f.processing_status = "failed"
        out(T.RECEIPT_FAILED)
        _cleanup(storage, f, payload)
        return
    if "not a receipt" in ro.warnings and not ro.lines:
        f.processing_status = "rejected"
        out(T.RECEIPT_NOT_RECEIPT)
        _cleanup(storage, f, payload)
        return
    r = Receipt(customer_id=c.id, file_id=f.id, store_name=ro.store_name, currency=ro.currency, subtotal=ro.subtotal,
                tax=ro.tax, total=ro.total, warnings=ro.warnings, extraction_version=ro.extraction_version,
                is_mock=ro.is_mock)
    s.add(r)
    s.flush()
    parsed, unclear = ParsedList(), 0
    for ln in ro.lines:
        conf = Decimal(str(ln.confidence))
        review = ln.needs_review or conf < MIN_CONFIDENCE
        s.add(ReceiptLine(customer_id=c.id, receipt_id=r.id, raw_text=redact(ln.raw_text)[:500], name=ln.name, brand=ln.brand,
                          quantity=ln.quantity, unit=ln.unit, pack_size=ln.pack_size, unit_price=ln.unit_price,
                          line_total=ln.line_total, confidence=conf, needs_review=review))
        if review or not ln.name:
            unclear += 1
            continue
        name = ln.name.strip().lower()[:120]
        parsed.items.append(ParsedItem(name=name, sku=match_staple(name), quantity=ln.quantity or Decimal(1),
                                       confidence=float(conf)))
    f.processing_status = "done"
    b = bk.open_basket(s, c)
    # A receipt is evidence of past purchases, not this week's needs: suggest, never add silently.
    bk.apply_parsed(s, b, parsed, "receipt", str(r.id), suggest=True)
    store = f" from {ro.store_name}" if ro.store_name else ""
    from app import agent
    from app.services.inbound import agent_turn
    if agent.enabled() and not ro.is_mock:  # let the agent read it back like a person, flagging what was unclear
        sug = bk.lines_of(s, b, suggested=True)
        summary = (f"[receipt read{store}: {len(sug)} items: " + "; ".join(f"{n}) {l.name}" for n, l in enumerate(sug, 1))
                   + (f". {unclear} lines were unreadable." if unclear else ".") +
                   (" Warnings: " + "; ".join(ro.warnings[:3]) if ro.warnings else "") +
                   "] These are past purchases saved as numbered suggestions. Ask which they need this time.")
        agent_turn(s, c, inp, summary, f"rc:{inp.id}:{payload['n']}")
        _cleanup(storage, f, payload, keep=True)
        return
    msg = ("MOCK (AI off): " if ro.is_mock else "") + T.pick_list(bk.lines_of(s, b, suggested=True), store)
    if unclear:
        msg += f" {unclear} unclear lines skipped."
    out(msg)
    _cleanup(storage, f, payload, keep=True)
    log.info("receipt processed for %s: %d lines", mask_phone(c.phone), len(ro.lines))


def is_audio(mime: str) -> bool:
    return mime.split(";")[0].strip().lower().startswith("audio/")


def _voice_note(s, c, inp, payload, storage, out):
    """MMS voice note: transcribe, then treat the words exactly like a typed list. Audio is not kept."""
    cfg = get_settings()
    since = utcnow() - timedelta(days=1)
    if s.scalar(select(func.count(Event.id)).where(Event.customer_id == c.id, Event.kind == "voice_note",
                                                   Event.created_at > since)) >= cfg.daily_receipt_limit:
        out(T.VOICE_LIMIT)
        return
    mime = payload.get("type", "").split(";")[0].strip().lower()
    if mime not in AUDIO_FORMATS:
        out(T.VOICE_BAD_FORMAT)
        _cleanup_media(storage, payload)
        return
    try:
        data = storage.get(payload["demo_key"]) if payload.get("demo_key") else fetch_media(payload["url"], 3_000_000)
        t = get_transcriber()
        text = t.transcribe(data, mime)
    except Exception as e:
        log.warning("voice note failed for %s: %s", mask_phone(c.phone), type(e).__name__)
        out(T.VOICE_FAILED)
        _cleanup_media(storage, payload)
        return
    s.add(Event(customer_id=c.id, kind="voice_note", data={"input_id": str(inp.id), "mock": t.is_mock}))
    _cleanup_media(storage, payload)
    if not text:
        out(T.VOICE_FAILED)
        return
    from app import agent
    from app.services.inbound import agent_turn
    if agent.enabled():  # the words go to the agent exactly as if the customer had typed them
        agent_turn(s, c, inp, f"[voice note, transcribed] {text}", f"rc:{inp.id}:{payload['n']}")
        return
    b = bk.open_basket(s, c)
    questions = bk.apply_parsed(s, b, RuleListParser().parse(text), "voice", str(inp.id))
    heard = ("MOCK (voice AI off) " if t.is_mock else "") + T.HEARD.format(text=text[:120].rstrip(" .!?"))
    out(f"{heard} {bk.summary(s, b)} " + (questions[0] if questions else "Reply to fix anything, or QUOTE."))


def _cleanup_media(storage, payload):
    if payload.get("demo_key"):
        storage.delete(payload["demo_key"])
    elif payload.get("url"):
        delete_twilio_media(payload["url"])


def _cleanup(storage, f, payload, keep=False):
    _cleanup_media(storage, payload)
    if not keep:
        storage.delete(f.storage_key)
        f.deleted_at = utcnow()


def purge_expired_files(session_factory, limit=50) -> int:
    """Delete stored files past their retention date. Returns how many were removed."""
    storage, n = get_storage(), 0
    with session_factory() as s:
        files = s.execute(select(StoredFile).where(StoredFile.deleted_at.is_(None),
                                                   StoredFile.retention_expires_at < utcnow()).limit(limit)).scalars().all()
        for f in files:
            storage.delete(f.storage_key)
            f.deleted_at = utcnow()
            n += 1
        s.commit()
    if n:
        log.info("purged %d expired files", n)
    return n
