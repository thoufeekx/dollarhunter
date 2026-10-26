import logging
import re
import secrets
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update

from app.catalog import STORE
from app.config import get_settings
from app.models import Approval, Basket, Customer, LedgerEntry, Offer, Order, Quote
from app.providers.prices import ensure_fixture_offers, get_price_source
from app.services import basket as bk
from app.services import sms_text as T
from app.util import english, hash_token, money, new_token, short, utcnow

log = logging.getLogger("dh.quote")
CENT = Decimal("0.01")
MAX_BAD_CODES = 5


def _q(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def open_quote(s, basket: Basket) -> Quote | None:
    return s.execute(select(Quote).where(Quote.basket_id == basket.id, Quote.customer_id == basket.customer_id,
                                         Quote.status == "open").order_by(Quote.created_at.desc()).limit(1)
                     ).scalar_one_or_none()


def build_quote(s, c: Customer, basket: Basket) -> tuple[Quote | None, str]:
    """Fixture pricing (no agent): every line priced at STORE from the fixture catalogue."""
    lines = bk.lines_of(s, basket)
    if not lines:
        return None, ""
    ensure_fixture_offers(s)
    src = get_price_source()
    picks = {}
    for l in lines:
        offers = src.offers_for(s, l.sku) if l.sku else []
        if offers:
            picks[str(l.id)] = (offers[0], next((o for o in offers if o.kind == "regular"), None))
    return _make_quote(s, c, basket, lines, picks, fixture=True)


def build_quote_from_offers(s, c: Customer, basket: Basket, chosen: dict, compare: list | None = None
                            ) -> tuple[Quote, str]:
    """Agent pricing: `chosen` maps line_id -> stored flyer Offer. Savings count only against that same flyer
    item's own 'was' price; an item without one is priced but adds no saving and no fee."""
    lines = bk.lines_of(s, basket)
    picks = {}
    for lid, o in chosen.items():
        reg = s.execute(select(Offer).where(Offer.source_type == o.source_type, Offer.sku == o.sku,
                                            Offer.kind == "regular")).scalar_one_or_none()
        picks[lid] = (o, reg)
    return _make_quote(s, c, basket, lines, picks, fixture=False, compare=compare)


def _make_quote(s, c, basket, lines, picks: dict, fixture: bool, compare: list | None = None) -> tuple[Quote, str]:
    now, cfg = utcnow(), get_settings()
    s.execute(update(Quote).where(Quote.basket_id == basket.id, Quote.status == "open").values(status="superseded"))
    out, items, baseline, expires = [], Decimal(0), Decimal(0), now + timedelta(hours=cfg.quote_ttl_hours)
    for l in lines:
        qty = l.quantity  # callers check bk.missing_quantity first: unknown quantities block quoting
        if str(l.id) not in picks:
            out.append({"line_id": str(l.id), "name": l.name, "qty": str(qty), "priced": False})
            continue
        best, regular = picks[str(l.id)]
        base = regular.price if regular else best.price
        total, base_total = _q(best.price * qty), _q(base * qty)
        items, baseline = items + total, baseline + base_total
        expires = min(expires, best.valid_until)
        out.append({"line_id": str(l.id), "name": l.name, "qty": str(qty), "priced": True, "store": best.store,
                    "product": best.product, "pack": best.pack_size,
                    "kind": "offer" if regular and regular.price > best.price else "regular",
                    "price": str(best.price), "regular": str(base), "total": str(total),
                    "regular_total": str(base_total), "offer_id": str(best.id),
                    "link": best.source_ref if (best.source_ref or "").startswith("http") else None,
                    "flyer_link": best.pack_size if (best.pack_size or "").startswith("http") else None,
                    "per_weight": any(u in (best.unit or "").lower() for u in ("/lb", "/kg", "/100"))})
    gross = baseline - items
    fee = _q(gross * cfg.fee_rate) if gross > 0 else Decimal(0)
    total = items + fee
    over = basket.budget is not None and total > basket.budget
    stores = sorted({x["store"] for x in out if x["priced"]})
    q = Quote(customer_id=c.id, basket_id=basket.id, basket_version=basket.version, lines=out,
              charges={"items": str(items), "fee": str(fee), "tax": "0.00", "delivery": "0.00", "other": "0.00",
                       "budget": str(basket.budget) if basket.budget is not None else None, "over_budget": over,
                       "compare": compare or []},
              baseline={"store": ", ".join(stores) or STORE, "regular_total": str(baseline),
                        "method": "same store regular price" if fixture else "same flyer item's regular price"},
              verification_status="fixture" if fixture else "flyer", complete=all(x["priced"] for x in out),
              expires_at=expires, gross_savings=gross, fee=fee, net_savings=gross - fee, total=total, is_fixture=fixture)
    s.add(q)
    s.flush()
    basket.status = "quoted"
    if over:  # above the budget cap: no approval code until the user raises the cap and re-quotes
        s.flush()
        return q, ""
    code = f"{secrets.randbelow(10**6):06d}"
    s.add(Approval(customer_id=c.id, quote_id=q.id, quote_basket_version=basket.version, idempotency_key="sms",
                   code_hash=hash_token(f"{q.id}:{code}"), expires_at=expires))
    s.flush()
    return q, code


def quote_text(q: Quote, code: str) -> str:
    """The quote as a short, readable SMS: one line per store, the customer's own item names, then totals."""
    priced = [x for x in q.lines if x["priced"]]
    if not priced:
        return ("I could not find those at stores near you this week. Try other words, or ask for options on one item."
                if not q.is_fixture else "We could not price any of those yet in the demo.")
    if q.is_fixture:
        head = f"Demo quote, {STORE}. Total {money(q.total)}"
    else:
        stores = {x["store"] for x in priced}
        head = (f"Best: all at {next(iter(stores))}" if len(stores) == 1 else
                f"Best trip: {len(stores)} stores") + f". Total {money(q.total)}"
    by_store: dict[str, list[str]] = {}
    for x in sorted(priced, key=lambda x: -Decimal(x["total"])):
        qty = Decimal(x["qty"]).normalize()
        hint = "" if q.is_fixture else _hint(x["name"], x.get("product") or "")
        item = (f"{'' if qty == 1 else f'{qty:f} '}{x['name']}{f' ({hint})' if hint else ''} "
                f"{money(Decimal(x['total']))}" + (" /lb" if x.get("per_weight") else ""))
        by_store.setdefault(x.get("store") or STORE, []).append(item)
    flyer = {x.get("store") or STORE: x.get("flyer_link") or x.get("link") for x in priced if x.get("flyer_link") or x.get("link")}
    body = [f"{st}: {', '.join(items)}" + (f" {flyer[st].removeprefix('https://')}" if flyer.get(st) else "")
            for st, items in sorted(by_store.items(), key=lambda kv: -len(kv[1]))]
    missing = [x["name"] for x in q.lines if not x["priced"]]
    if missing:
        body.append("Not found: " + ", ".join(missing[:4]) + ("..." if len(missing) > 4 else ""))
    for c in q.charges.get("compare", []):
        if c.get("kind") == "one store":
            body.append(f"All at {c['store']}: {money(Decimal(c['total']))}")
        elif c.get("kind") == "cheapest anywhere":
            body.append(f"Cheapest anywhere: {money(Decimal(c['total']))} across {c['stops']} stores")
        elif c.get("kind") == "fewest stores":
            body.append(f"Fewest stops: {money(Decimal(c['total']))} at {c['stops']} store{'s' if c['stops'] > 1 else ''}")
    if q.gross_savings > 0:
        body.append(f"You save {money(q.gross_savings)} vs regular {money(Decimal(q.baseline['regular_total']))} "
                    f"(our fee {money(q.fee)}, 25% of savings)")
    else:
        body.append("No verified saving, so no fee")
    tail = "Tax and delivery $0 in demo." if q.is_fixture else "Prices before tax."
    if q.charges.get("over_budget"):
        tail += f" Over your {money(Decimal(q.charges['budget']))} budget: remove items or reply BUDGET and an amount."
    elif q.is_fixture:
        tail += f" Reply YES {code} to approve (demo order)."
    if not q.is_fixture and any(x.get("link") for x in priced):
        tail += " Reply LINKS for each item's own link."
    return "\n".join([head, *body, tail])


HINT_SKIP = {"or", "and", "&", "|", "-", "with", "ml", "l", "g", "kg", "lb", "lbs", "oz", "pk", "pack", "ea", "each",
             "selected", "varieties", "assorted", "asst", "size", "large", "regular"}


def _hint(want: str, product: str) -> str:
    """A few words of the real product that the customer's own wording leaves out: "toothpaste" -> "Colgate or Crest"."""
    have = {w.rstrip("s") for w in want.lower().split()}
    words = [w for w in (re.sub(r"[^\w'&-]", "", w) for w in english(product).replace(",", " ").split())
             if w and w.lower().rstrip("s") not in have
             and not any(ch.isdigit() for ch in w) and w.lower().strip(".") not in HINT_SKIP]
    cap = lambda w: w.capitalize() if w.isupper() else w
    m = re.match(r"^\s*(\w+)\s+or\s+(\w+)\b", english(product), re.I)  # "COLGATE OR CREST TOOTHPASTE"
    hint = f"{cap(m.group(1))} or {cap(m.group(2))}" if m else " ".join(cap(w.strip("™®")) for w in words[:2])
    return hint[:24]


def links_text(q: Quote, item: str | None = None) -> str:
    """Direct flyer or store links for the quote's items (all, or the one named)."""
    rows = [x for x in q.lines if x.get("priced") and x.get("link")]
    if item:
        rows = [x for x in rows if item.lower() in x["name"].lower()] or rows
    if not rows:
        return "No flyer links for these items: their prices came from online listings."
    return "Flyer links:\n" + "\n".join(f"{x['name']} ({x['store']}): {x['link'].removeprefix('https://')}" for x in rows[:8])


def check_quote(s, c: Customer) -> tuple[Basket | None, Quote | None, str | None]:
    """Find the customer's approvable quote, or the reason there is none."""
    b = s.execute(select(Basket).where(Basket.customer_id == c.id, Basket.status.in_(["draft", "confirmed", "quoted"]))
                  ).scalar_one_or_none()
    q = open_quote(s, b) if b else None
    if not q:
        return b, None, T.NO_QUOTE
    if q.basket_version != b.version:
        q.status = "stale"
        return b, None, T.STALE_QUOTE
    if q.expires_at < utcnow():
        q.status = "expired"
        return b, None, T.EXPIRED_QUOTE
    if q.charges.get("over_budget"):
        return b, None, T.OVER_BUDGET
    return b, q, None


def approve_with_code(s, c: Customer, code: str) -> str:
    b, q, err = check_quote(s, c)
    if err:
        return err
    a = s.execute(select(Approval).where(Approval.quote_id == q.id, Approval.customer_id == c.id,
                                         Approval.code_hash == hash_token(f"{q.id}:{code}"),
                                         Approval.consumed_at.is_(None)).with_for_update()).scalar_one_or_none()
    if not a:
        a = s.execute(select(Approval).where(Approval.quote_id == q.id, Approval.idempotency_key == "sms")
                      .with_for_update()).scalar_one_or_none()
        bad = (a.consent_snapshot or {}).get("bad_codes", 0) + 1 if a else MAX_BAD_CODES
        if a:
            a.consent_snapshot = {**(a.consent_snapshot or {}), "bad_codes": bad}
        if bad >= MAX_BAD_CODES:
            q.status = "locked"
            return T.LOCKED_QUOTE
        return T.BAD_CODE
    return _place(s, c, b, q, a, channel="sms")


def _place(s, c, b, q, a, channel: str) -> str:
    now = utcnow()
    a.consumed_at = now
    a.consent_snapshot = {"channel": channel, "total": str(q.total), "fee": str(q.fee), "basket_version": b.version,
                          "simulated": True, "at": now.isoformat()}
    o = Order(customer_id=c.id, quote_id=q.id, approval_id=a.id, mode="simulated", status="simulated_placed",
              delivery={"store": STORE, "note": "simulated: nothing is bought or delivered"})
    s.add(o)
    s.flush()
    q.status, b.status, b.closed_at = "approved", "ordered", now
    s.flush()
    log.info("simulated order %s placed via %s", o.id, channel)
    saved = f", est. saving {money(q.net_savings)} after fee" if q.gross_savings > 0 else ""
    return (f"DEMO: simulated order, no purchase or delivery booked. Total {money(q.total)}{saved}. "
            "Text a new list any time.")


def complete_order(s, o: Order) -> LedgerEntry | None:
    """Simulated delivery: only now does the estimated saving become a (simulated) ledger entry."""
    if o.status != "simulated_placed":
        return None
    q = s.get(Quote, o.quote_id)
    o.status = "simulated_delivered"
    e = LedgerEntry(customer_id=o.customer_id, order_id=o.id, entry_type="completion", gross=q.gross_savings,
                    fee=q.fee, net=q.net_savings, status="simulated")
    s.add(e)
    s.flush()
    return e


def cancel_order(s, o: Order) -> LedgerEntry | None:
    """Cancel; a completed order gets a reversing entry. Audit history is never deleted."""
    if o.status == "cancelled":
        return None
    done = s.execute(select(LedgerEntry).where(LedgerEntry.order_id == o.id, LedgerEntry.entry_type == "completion")
                     ).scalar_one_or_none()
    o.status = "cancelled"
    if not done:
        s.flush()
        return None
    r = LedgerEntry(customer_id=o.customer_id, order_id=o.id, entry_type="reversal", gross=-done.gross,
                    fee=-done.fee, net=-done.net, status="simulated", reverses_id=done.id)
    s.add(r)
    s.flush()
    return r
