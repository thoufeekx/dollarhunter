"""Current grocery flyer prices near a postal code, from Flipp's public flyer search.

There is no official Flipp API: this uses the endpoint the Flipp app itself calls, gently (one search per
item and area, cached for FLYER_HOURS). It can change or be blocked at any time, so it sits behind the
PriceSource interface and every price it returns is stored as an Offer with its flyer item id and dates.
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select

from app.models import Offer
from app.providers.places import UA, cache_get, cache_put
from app.util import utcnow

log = logging.getLogger("dh.flipp")
SEARCH = "https://backflipp.wishabi.com/flipp/items/search"
FLYER_HOURS = 6
NOT_GROCERY = {"staples", "princess auto", "canadian tire", "home depot", "rona", "best buy", "the brick", "ikea",
               "michaels", "dollarama", "toys r us", "petsmart", "pet valu", "sport chek", "mark's", "home hardware",
               "bureau en gros", "bureau en gros/staples", "canadian tire ", "jysk", "lowe's", "structube", "the source"}
MAX_RESULTS = 12


_last = [0.0]
_lock = threading.Lock()
MIN_GAP = 0.3  # seconds between live searches, to stay polite


def _get(params: dict) -> httpx.Response:
    """Polite, retried GET: spaced requests, backoff on rate limits and server errors."""
    for wait in (2, 5, None):
        with _lock:
            gap = MIN_GAP - (time.monotonic() - _last[0])
            if gap > 0:
                time.sleep(gap)
            _last[0] = time.monotonic()
        r = httpx.get(SEARCH, params=params, headers={"User-Agent": UA}, timeout=20)
        if r.status_code in (403, 429, 500, 502, 503, 504) and wait:
            log.info("flipp %s, retrying in %ss", r.status_code, wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r


def _dec(v) -> Decimal | None:
    try:
        d = Decimal(str(v))
        return d if d > 0 else None
    except (InvalidOperation, TypeError):
        return None


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def search(s, postal: str, query: str, merchants: set[str] | None = None) -> list[dict]:
    """Flyer deals for `query` near `postal` (cheapest first), then regular online prices as a fallback."""
    q = " ".join(query.lower().split())[:60]
    postal = postal.replace(" ", "").upper()
    if len(postal) == 3:  # Flipp needs a full code; flyers are chosen by area, so any code in the area works
        postal += "1A1"
    key = f"flipp:{postal[:3]}:{q}"
    hit = cache_get(s, key) or {}
    items = hit.get("items") if hit.get("v") == 2 else None  # v1 entries had colliding online ids
    if items is None:
        r = _get({"locale": "en-ca", "postal_code": postal, "q": q})
        items, data = [], r.json()
        for x in data.get("items", []):  # this week's flyer deals valid for this postal area
            price = _dec(x.get("current_price"))
            if not price or not x.get("name") or not x.get("valid_to"):
                continue
            items.append({"id": str(x["id"]), "store": x.get("merchant_name") or "?", "name": x["name"][:120],
                          "price": str(price), "was": str(_dec(x.get("original_price")) or ""),
                          "unit": (x.get("post_price_text") or "").strip()[:30], "kind": "flyer",
                          "link": f"https://flipp.com/en-ca/item/{x['id']}",
                          "flyer_link": f"https://flipp.com/en-ca/flyer/{x['flyer_id']}" if x.get("flyer_id") else None,
                          "valid_from": x.get("valid_from"), "valid_to": x["valid_to"]})
        until = (utcnow() + timedelta(days=1)).isoformat()
        for x in data.get("ecom_items", [])[:40]:  # regular online prices (mostly walmart.ca) when no flyer has it
            price = _dec(x.get("current_price"))
            if not price or not x.get("name"):
                continue
            gid = x.get("global_id_int") or x.get("global_id") or f"{x.get('merchant_id')}-{x.get('sku')}"
            items.append({"id": f"e{gid}", "store": x.get("merchant") or "?", "name": x["name"][:120],
                          "price": str(price), "was": str(_dec(x.get("original_price")) or ""), "unit": "",
                          "kind": "online", "valid_from": None, "valid_to": until})
        cache_put(s, key, {"items": items, "v": 2}, timedelta(hours=FLYER_HOURS))
        log.info("flipp search %r near %s: %d priced items", q, postal[:3], len(items))
    now = utcnow()
    items = [i for i in items if _ts(i["valid_to"]) > now and (not merchants or i["store"] in merchants)
             and i["store"].lower() not in NOT_GROCERY]
    items.sort(key=lambda i: (i.get("kind") != "flyer", Decimal(i["price"])))
    flyer = [i for i in items if i.get("kind") == "flyer"][:MAX_RESULTS]
    online = [i for i in items if i.get("kind") != "flyer"][:MAX_RESULTS]
    return flyer + online


def remember(s, item: dict) -> Offer:
    """Persist a flyer item the agent chose as an Offer (plus its 'was' price as the regular-price reference),
    so a quote is computed from stored evidence, never from numbers the model typed."""
    o = s.execute(select(Offer).where(Offer.source_type == item.get("source", "flipp"), Offer.sku == item["id"],
                                      Offer.kind == "offer")
                  ).scalar_one_or_none()
    if o:
        if item.get("link") and not (o.source_ref or "").startswith("http"):
            o.source_ref = item["link"]  # rows saved before links were recorded
        if item.get("flyer_link") and not (o.pack_size or "").startswith("http"):
            o.pack_size = item["flyer_link"][:60]
        return o
    common = dict(store=item["store"][:80], sku=item["id"][:40], product=item["name"][:120],
                  unit=(item["unit"] or "each")[:30],
                  equivalence_key=item["id"],
                  valid_from=_ts(item["valid_from"]) if item.get("valid_from") else utcnow(),
                  valid_until=_ts(item["valid_to"]), source_type=item.get("source", "flipp"),
                  source_ref=(item.get("link") or f"flipp {item.get('kind', 'flyer')} item {item['id']}")[:200],
                  pack_size=(item.get("flyer_link") or item["unit"] or "")[:60],
                  fetched_at=utcnow(), is_fixture=False)
    o = Offer(price=Decimal(item["price"]), kind="offer", **common)
    s.add(o)
    if item.get("was") and Decimal(item["was"]) > o.price:
        s.add(Offer(price=Decimal(item["was"]), kind="regular", **common))
    s.flush()
    return o
