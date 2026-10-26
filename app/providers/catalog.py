"""Area price catalog: every grocery flyer for a postal area, downloaded in bulk once or twice a day, so
shopping questions are answered from our own database in milliseconds instead of live searches.

Source: Flipp's public flyer listing (the same unofficial endpoints the Flipp app uses). A flyer's
`discount` is a percent off, from which the regular price is derived; items without it carry no regular price.
"""
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx
from sqlalchemy import delete, func, or_, select

from app.models import FlyerItem
from app.providers.flipp import NOT_GROCERY
from app.providers.places import UA, cache_get, cache_put
from app.util import utcnow

log = logging.getLogger("dh.catalog")
BASE = "https://backflipp.wishabi.com/flipp"
REFRESH_HOURS = 12
CATEGORIES = {"Groceries", "Pharmacy"}  # pharmacies carry toothpaste, tissue, snacks
PER_STORE = 6


def _code(postal_or_fsa: str) -> str:
    p = postal_or_fsa.replace(" ", "").upper()
    return p if len(p) == 6 else p[:3] + "1A1"  # Flipp needs a full code; flyers are chosen by area


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def _get(path: str, **params) -> dict | list:
    r = httpx.get(f"{BASE}/{path}", params={"locale": "en-ca", **params}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.json()


def fresh(s, fsa: str) -> bool:
    return cache_get(s, f"catalog:{fsa}") is not None


def refresh_area(s, postal_or_fsa: str) -> int:
    """Download all current grocery and pharmacy flyers for the area. Returns the number of items stored."""
    fsa = postal_or_fsa.replace(" ", "").upper()[:3]
    data = _get("flyers", postal_code=_code(postal_or_fsa))
    flyers = [f for f in (data if isinstance(data, list) else data.get("flyers", []))
              if CATEGORIES & set(f.get("categories") or [])]
    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(lambda f: (f, _safe_items(f["id"])), flyers))
    now, n = utcnow(), 0
    s.execute(delete(FlyerItem).where(FlyerItem.fsa == fsa))
    for f, items in pages:
        for i in items:
            price = _dec(i.get("price"))
            if not price or not i.get("name") or not i.get("valid_to"):
                continue
            off = _dec(i.get("discount"))
            regular = (price / (1 - off / 100)).quantize(Decimal("0.01"), ROUND_HALF_UP) if off and 0 < off < 90 else None
            s.add(FlyerItem(id=f"{fsa}:{i['id']}", fsa=fsa, flyer_id=str(f["id"]), store=f.get("merchant", "?")[:80],
                            name=" ".join(i["name"].split())[:200], brand=(i.get("brand") or None) and i["brand"][:120],
                            price=price, regular=regular, valid_from=_ts(i.get("valid_from")),
                            valid_to=_ts(i["valid_to"]), fetched_at=now))
            n += 1
    s.flush()
    cache_put(s, f"catalog:{fsa}", {"items": n, "flyers": len(flyers)}, timedelta(hours=REFRESH_HOURS))
    log.info("catalog %s: %d items from %d flyers", fsa, n, len(flyers))
    return n


def _safe_items(flyer_id) -> list:
    try:
        d = _get(f"flyers/{flyer_id}")
        return d.get("items", []) if isinstance(d, dict) else []
    except httpx.HTTPError as e:
        log.warning("flyer %s failed: %s", flyer_id, type(e).__name__)
        return []


def _dec(v):
    try:
        d = Decimal(str(v))
        return d if d > 0 else None
    except (InvalidOperation, TypeError):
        return None


def search(s, fsa: str, words: list[str], limit: int = 60) -> list[dict]:
    """Current flyer items in the area whose name or brand contains any of the words; cheapest few per store."""
    words = [w.strip() for w in words if w and len(w.strip()) > 1][:6]
    if not words:
        return []
    pats = [r"\m" + re.escape(w.lower().rstrip("s")) + r"s?\M" for w in words]  # whole words, plural optional
    conds = [c for p in pats for c in (FlyerItem.name.op("~*")(p), FlyerItem.brand.op("~*")(p))]
    rows = s.execute(select(FlyerItem).where(FlyerItem.fsa == fsa.upper()[:3], FlyerItem.valid_to > func.now(),
                                             or_(*conds)).order_by(FlyerItem.price)).scalars().all()
    rows = [r for r in rows if r.store.lower() not in NOT_GROCERY]
    per, out, seen = {}, [], set()
    for r in rows:
        key = (r.store, r.name.lower(), r.price)
        if per.get(r.store, 0) >= PER_STORE or key in seen:
            continue
        seen.add(key)
        per[r.store] = per.get(r.store, 0) + 1
        out.append({"id": r.id, "store": r.store, "name": r.name + (f" ({r.brand})" if r.brand and r.brand.lower()
                    not in r.name.lower() else ""), "price": str(r.price), "was": str(r.regular or ""), "unit": "",
                    "kind": "flyer", "source": "flyer", "link": f"https://flipp.com/en-ca/item/{r.id.split(':', 1)[1]}",
                    "flyer_link": f"https://flipp.com/en-ca/flyer/{r.flyer_id}",
                    "valid_from": r.valid_from.isoformat() if r.valid_from else None,
                    "valid_to": r.valid_to.isoformat()})
        if len(out) >= limit:
            break
    return out


_running: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def refresh_in_background(postal: str):
    """Start the area download in a thread so customer messages never queue behind it."""
    from app.db import session_factory
    fsa = postal.replace(" ", "").upper()[:3]

    def work():
        try:
            with session_factory()() as s:
                if not fresh(s, fsa):
                    refresh_area(s, postal)
                    s.commit()
                _cache_store_names(s, postal)
                s.commit()
                from app.providers.places import lookup_postal, nearby_stores
                if cache_get(s, f"stores:{fsa}:5") is None and (area := lookup_postal(postal)):
                    nearby_stores(s, area)  # store addresses from OpenStreetMap: slow, so only in background
                    s.commit()
        except Exception as e:
            log.warning("background refresh %s failed: %s", fsa, type(e).__name__)
        finally:
            with _lock:
                _running.pop(fsa, None)

    with _lock:
        if fsa not in _running:
            _running[fsa] = threading.Thread(target=work, name=f"catalog-{fsa}", daemon=True)
            _running[fsa].start()


def wait_for(fsa: str, seconds: float = 25):
    t = _running.get(fsa.upper()[:3])
    if t:
        t.join(seconds)


def store_names(s, postal: str) -> list[str]:
    """Chains with a current grocery flyer for this postal code (fast, and always matches what we can price)."""
    fsa = postal.replace(" ", "").upper()[:3]
    if (hit := cache_get(s, f"chains:{fsa}")) is not None:
        return hit["chains"]
    return _cache_store_names(s, postal)


def _cache_store_names(s, postal: str) -> list[str]:
    fsa = postal.replace(" ", "").upper()[:3]
    data = _get("flyers", postal_code=_code(postal))
    chains = []
    for f in data if isinstance(data, list) else data.get("flyers", []):
        if "Groceries" in (f.get("categories") or []) and f.get("merchant") not in chains:
            chains.append(f["merchant"])
    cache_put(s, f"chains:{fsa}", {"chains": chains}, timedelta(hours=REFRESH_HOURS))
    return chains
