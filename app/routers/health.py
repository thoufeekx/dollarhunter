from functools import lru_cache

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.config import get_settings
from app.db import engine

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/healthz")
def healthz():
    s = get_settings()
    try:
        with engine().connect() as c:
            c.execute(text("select 1"))
        db = "ok"
    except Exception:
        db = "down"
    return {"status": "ok" if db == "ok" else "degraded", "db": db, "env": s.env, "demo_mode": s.demo_mode,
            "sms": s.effective_sms_provider, "ai": s.effective_ai_provider, "prices": s.price_source,
            "storage": s.storage_backend, "payments": False, "real_orders": False}


@lru_cache
def _qr_svg(link: str) -> str:
    import segno
    return segno.make(link, error="m").svg_inline(scale=5, dark="#0b3d2a", light=None, border=1)


def _pretty(e164: str) -> str:
    d = e164[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if e164.startswith("+1") and len(e164) == 12 else e164


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    s = get_settings()
    ctx = {"s": s, "sms_link": None}
    if s.effective_sms_provider == "twilio":
        link = f"sms:{s.twilio_from_number}?&body=Hello"
        ctx.update(sms_link=link, number=_pretty(s.twilio_from_number), qr=_qr_svg(link))
    return templates.TemplateResponse(request, "landing.html", ctx)
