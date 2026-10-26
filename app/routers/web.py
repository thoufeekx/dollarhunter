import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.config import get_settings
from app.db import get_db
from app.security import require_demo
from app.models import Customer, Input, Message, Order
from app.services import quote as qt
from app.services.inbound import E164, record_inbound
from app.storage import get_storage

router = APIRouter()
templates = Jinja2Templates(directory="templates")
PRIVATE = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


def _page(request, name, ctx, status=200):
    return templates.TemplateResponse(request, name, {"s": get_settings(), **ctx}, status_code=status, headers=PRIVATE)


@router.get("/demo", response_class=HTMLResponse)
def demo_page(request: Request):
    if not get_settings().demo_mode:
        raise HTTPException(404)
    return _page(request, "demo.html", {})


def _demo_phone(db, phone: str):
    if not E164.match(phone):
        raise HTTPException(422, "phone must be E.164, like +14165550101")
    c = db.execute(select(Customer).where(Customer.phone == phone)).scalar_one_or_none()
    if c and not c.is_demo:
        raise HTTPException(409, "That number belongs to a real customer. Use a demo number.")
    return c


@router.get("/demo/conversation", dependencies=[Depends(require_demo)])
def demo_conversation(phone: str, db=Depends(get_db)):
    c = _demo_phone(db, phone)
    if not c:
        return {"messages": [], "orders": []}
    ins = db.execute(select(Input).where(Input.customer_id == c.id)).scalars().all()
    outs = db.execute(select(Message).where(Message.customer_id == c.id)).scalars().all()
    rows = [{"dir": "in", "text": i.text or ("[file]" if i.num_media else ""), "at": i.received_at.isoformat()} for i in ins]
    rows += [{"dir": "out", "text": m.body, "at": m.created_at.isoformat(), "status": m.status,
              "segments": m.segments} for m in outs]
    orders = db.execute(select(Order).where(Order.customer_id == c.id).order_by(Order.created_at)).scalars().all()
    return {"messages": sorted(rows, key=lambda r: r["at"]), "label": "DEMO: simulated texts, nothing is sent",
            "orders": [{"id": str(o.id), "status": o.status} for o in orders]}


@router.post("/demo/receipt", dependencies=[Depends(require_demo)])
async def demo_receipt(phone: str = Form(...), file: UploadFile = File(...), db=Depends(get_db)):
    _demo_phone(db, phone)
    data = await file.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(413, "file too large")
    key = f"incoming/{uuid.uuid4()}"
    get_storage().put(key, data)
    i, _ = record_inbound(db, "demo", uuid.uuid4().hex, phone, "", 1, demo=True,
                          media=[{"demo_key": key, "type": file.content_type or ""}])
    db.commit()
    return {"input_id": str(i.id), "label": "DEMO: simulated receipt upload"}


@router.post("/demo/orders/{order_id}/{action}", dependencies=[Depends(require_demo)])
def demo_order(order_id: uuid.UUID, action: str, db=Depends(get_db)):
    o = db.get(Order, order_id)
    if not o or action not in {"complete", "cancel"}:
        raise HTTPException(404)
    e = qt.complete_order(db, o) if action == "complete" else qt.cancel_order(db, o)
    return JSONResponse({"status": o.status, "ledger_entry": e.entry_type if e else None,
                         "label": "DEMO: simulated outcome, nothing was delivered"})
