import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from twilio.request_validator import RequestValidator

from app.config import get_settings
from app.db import get_db
from app.security import RateLimiter, require_demo
from app.services.inbound import record_inbound

router = APIRouter()
log = logging.getLogger("dh.sms")
limiter = RateLimiter(120, 60)
EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def _ok():
    return Response(EMPTY, media_type="application/xml")


@router.post("/sms/inbound")
async def sms_inbound(request: Request, db=Depends(get_db)):
    s = get_settings()
    limiter.check(request.client.host if request.client else "x")
    form = await request.form()
    params = {k: v for k, v in form.items() if isinstance(v, str)}
    if not s.twilio_auth_token:
        raise HTTPException(503, "Inbound SMS not configured")
    url = s.public_base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    sig = request.headers.get("X-Twilio-Signature", "")
    if not RequestValidator(s.twilio_auth_token).validate(url, params, sig):
        raise HTTPException(403, "Invalid signature")
    sid, acct, frm = params.get("MessageSid"), params.get("AccountSid"), params.get("From")
    if not (sid and acct and frm):
        raise HTTPException(400, "Missing fields")
    if s.twilio_account_sid and acct != s.twilio_account_sid:
        raise HTTPException(403, "Wrong account")
    n = int(params.get("NumMedia", "0") or 0)
    media = [{"url": params[f"MediaUrl{i}"], "type": params.get(f"MediaContentType{i}", "")}
             for i in range(min(n, 10)) if params.get(f"MediaUrl{i}")]
    try:
        record_inbound(db, "twilio", f"{acct}:{sid}", frm, params.get("Body", ""), n, media=media)
    except ValueError:
        raise HTTPException(400, "Bad sender")
    db.commit()
    return _ok()


class DemoInbound(BaseModel):
    phone: str
    text: str
    message_id: str | None = None


@router.post("/demo/inbound", dependencies=[Depends(require_demo)])
def demo_inbound(body: DemoInbound, db=Depends(get_db)):
    import uuid

    from app.routers.web import _demo_phone
    _demo_phone(db, body.phone)
    ext = body.message_id or uuid.uuid4().hex
    try:
        i, created = record_inbound(db, "demo", ext, body.phone, body.text, demo=True)
    except ValueError:
        raise HTTPException(422, "phone must be E.164")
    db.commit()
    return {"created": created, "input_id": str(i.id) if i else None, "label": "DEMO: simulated inbound text"}
