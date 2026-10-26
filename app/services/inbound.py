import itertools
import logging
import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import agent
from app.catalog import SERVICE_PREFIXES
from app.models import Customer, Input
from app.providers.fixtures import GREETING, RuleListParser
from app.services import basket as bk
from app.services import quote as qt
from app.services import sms_text as T
from app.services.jobs import enqueue_job
from app.services.outbox import enqueue_message, suppress_queued
from app.util import mask_phone, money, utcnow

log = logging.getLogger("dh.inbound")
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "yes", "unstop"}
POSTAL = re.compile(r"^\s*([A-Za-z]\d[A-Za-z])\s*\d?[A-Za-z]?\d?\s*$")
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
APPROVE = re.compile(r"^\s*(?:yes|approve|ok|y)[\s:,-]*(\d{6})\s*$", re.I)
QUOTE_WORDS = {"quote", "price", "prices", "total", "checkout", "done", "ready", "thats all", "that's all", "order"}
SHOW_WORDS = {"basket", "list", "show", "cart", "edit"}
CLEAR_WORDS = {"clear", "reset", "start over", "new list", "empty"}
FEE_WORDS = {"fee", "fees", "pricing", "cost", "how much"}
ADD = re.compile(r"^\s*add\s+(all|[\d\s,&and]+)\s*$", re.I)
BUDGET = re.compile(r"^\s*budget\s*:?\s*\$?\s*(\d{1,4}(?:\.\d{1,2})?)\s*$", re.I)
MAX_MEDIA = 3


def get_or_create_customer(s, phone: str, demo=False) -> Customer:
    c = s.execute(select(Customer).where(Customer.phone == phone)).scalar_one_or_none()
    if c:
        return c
    try:
        with s.begin_nested():
            c = Customer(phone=phone, is_demo=demo, preferences={"substitutions": "ask"})
            s.add(c)
            s.flush()
        return c
    except IntegrityError:
        return s.execute(select(Customer).where(Customer.phone == phone)).scalar_one()


def record_inbound(s, channel: str, external_id: str, phone: str, text: str, num_media=0, demo=False, media=None):
    """Persist input + job atomically. Returns (input, created). media: [{"url"|"demo_key", "type"}]."""
    if not E164.match(phone):
        raise ValueError("invalid phone")
    c = get_or_create_customer(s, phone, demo)
    try:
        with s.begin_nested():
            i = Input(customer_id=c.id, channel=channel, external_id=external_id, text=text, num_media=num_media)
            s.add(i)
            s.flush()
    except IntegrityError:
        return None, False
    enqueue_job(s, "process_inbound", f"inbound:{channel}:{external_id}", c.id,
                {"input_id": str(i.id), "media": (media or [])[:MAX_MEDIA]})
    return i, True


def handle_process_inbound(s, payload: dict):
    inp = s.get(Input, payload["input_id"])
    if not inp or inp.processing_status == "done":
        return
    c = s.get(Customer, inp.customer_id)
    key = f"in:{inp.id}"
    text = (inp.text or "").strip()
    word = text.lower().strip(" .!?")
    media = payload.get("media") or []
    out = lambda body, kind="reply", allow=False, n=1: enqueue_message(s, c, body, kind, f"{key}:{n}", allow)

    if word in STOP_WORDS:
        c.opted_out, c.sms_consent_state, c.sms_consent_at = True, "revoked", utcnow()
        suppress_queued(s, c.id)
        out(T.STOP_ACK, "stop_ack", allow=True)
        media = []
    elif word == "help":
        out(T.HELP)
    elif word in START_WORDS and (c.opted_out or c.onboarding_state == "new"):
        was_new = c.onboarding_state == "new"
        c.opted_out, c.sms_consent_state, c.sms_consent_source, c.sms_consent_at = False, "granted", "sms_keyword", utcnow()
        if was_new:
            c.onboarding_state = "awaiting_postal"
            out(T.INTRO)
        else:
            out(T.START_ACK)
    elif c.opted_out:
        media = []
    elif m := APPROVE.match(text):
        out(qt.approve_with_code(s, c, m.group(1)), "approval")
    elif agent.enabled():
        if c.onboarding_state == "new":
            c.onboarding_state = "awaiting_postal"
            c.sms_consent_state, c.sms_consent_source, c.sms_consent_at = "transactional", "first_inbound", utcnow()
        if text:
            agent_turn(s, c, inp, text, key)
    elif c.onboarding_state == "new":
        c.sms_consent_state, c.sms_consent_source, c.sms_consent_at = "transactional", "first_inbound", utcnow()
        if m := POSTAL.match(text):
            c.postal_prefix, c.onboarding_state = m.group(1).upper(), "ready"
            out(T.WELCOME_READY if c.postal_prefix in SERVICE_PREFIXES else T.OUT_OF_AREA + " STOP to opt out.")
        else:
            c.onboarding_state = "awaiting_postal"
            out(T.INTRO)
            if text and not GREETING.match(text):
                _apply_list(s, c, inp, text, out, n0=2)
    elif c.onboarding_state == "awaiting_postal":
        m = POSTAL.match(text)
        if m:
            c.postal_prefix = m.group(1).upper()
            c.onboarding_state = "ready"
            out(T.ASK_LIST if c.postal_prefix in SERVICE_PREFIXES else T.OUT_OF_AREA)
        elif text and not GREETING.match(text):
            _apply_list(s, c, inp, text, out, n0=1)
            out(T.BAD_POSTAL, n=9)
        elif not media:
            out(T.BAD_POSTAL, n=9)
    elif word in {"yes", "y", "approve", "ok"}:
        out(T.NEED_CODE)
    elif word in QUOTE_WORDS:
        _quote(s, c, out)
    elif word in SHOW_WORDS:
        b = bk.open_basket(s, c)
        sug = bk.lines_of(s, b, suggested=True)
        out(bk.summary(s, b) + (f" {T.pick_list(sug)}" if sug else ""), "basket_summary")
    elif m := ADD.match(text):
        out(_add_suggestions(s, c, m.group(1)), "basket_summary")
    elif word in CLEAR_WORDS:
        bk.clear(s, bk.open_basket(s, c))
        out(T.CLEARED)
    elif word in FEE_WORDS:
        out(T.PRICING)
    elif m := BUDGET.match(text):
        amount = Decimal(m.group(1))
        bk.set_budget(s, bk.open_basket(s, c), amount)
        out(T.BUDGET_SET.format(amount=money(amount)))
    elif text:
        _apply_list(s, c, inp, text, out, n0=1)
    for n, m in enumerate(media):
        enqueue_job(s, "process_receipt", f"receipt:{inp.id}:{n}", c.id, {"input_id": str(inp.id), "n": n, **m})
    inp.processing_status = "done"
    log.info("processed inbound for %s", mask_phone(c.phone))


def agent_turn(s, c, inp, text: str, key: str):
    """Let the agent answer. On an unexpected error it gets one more try; if that also fails the customer gets
    their real basket back. Their words are never parsed by rules into basket items."""
    n = itertools.count(1)
    out = lambda body, kind="agent", allow=False: enqueue_message(s, c, body, kind, f"{key}:a{next(n)}", allow)
    for attempt in (1, 2):
        try:
            with s.begin_nested():
                agent.run(s, c, inp, text, out)
            return
        except Exception as e:
            log.warning("agent attempt %d failed for %s: %s", attempt, mask_phone(c.phone), type(e).__name__,
                        exc_info=True)
    out(bk.summary(s, bk.open_basket(s, c)) + " Something went wrong on my side just now; text me again in a moment.")


def _quote(s, c, out):
    b = bk.open_basket(s, c)
    if not bk.lines_of(s, b):
        sug = bk.lines_of(s, b, suggested=True)
        out(T.pick_list(sug) if sug else T.EMPTY_BASKET)
        return
    if missing := bk.missing_quantity(s, b):
        names = ", ".join(missing[:4]) + ("..." if len(missing) > 4 else "")
        out(T.NEED_QTY.format(names=names, example=f"2 {missing[0]}"))
        return
    q, code = qt.build_quote(s, c, b)
    out(qt.quote_text(q, code), "quote")


def _add_suggestions(s, c, which: str) -> str:
    """ADD 1 3 / ADD ALL: accept receipt suggestions by their number in the last list sent."""
    b = bk.open_basket(s, c)
    sug = bk.lines_of(s, b, suggested=True)
    if not sug:
        return T.NO_SUGGESTIONS
    picks = range(1, len(sug) + 1) if which.strip().lower() == "all" else {int(n) for n in re.findall(r"\d+", which)}
    for n in picks:
        if 1 <= n <= len(sug):
            bk.set_quantity(s, b, sug[n - 1], sug[n - 1].quantity or Decimal(1))
    return bk.summary(s, b) + " Reply QUOTE for prices."


def _apply_list(s, c, inp, text, out, n0=1):
    parsed = RuleListParser().parse(text)
    b = bk.open_basket(s, c)
    questions = bk.apply_parsed(s, b, parsed, "text", str(inp.id))
    body = bk.summary(s, b)
    if questions:
        body += " " + questions[0]
    elif c.onboarding_state == "ready":
        body += " Add more, or reply QUOTE for prices."
    out(body, "basket_summary", n=n0)
