"""Last-resort prices from grocery websites, read with Firecrawl, for items no flyer or online listing covers.

Store websites show their default store's regular price, not necessarily the customer's branch, and each page
costs Firecrawl credits, so results are cached for CACHE_DAYS and calls are capped per day.
"""
import hashlib
import logging
from datetime import timedelta
from urllib.parse import quote

import httpx
from sqlalchemy import func, select

from app.config import get_settings
from app.models import Event
from app.providers.places import cache_get, cache_put
from app.util import utcnow

log = logging.getLogger("dh.webprices")
SITES = {"Metro": "https://www.metro.ca/en/online-grocery/search?filter={q}",
         "No Frills": "https://www.nofrills.ca/en/search?search-bar={q}"}
CACHE_DAYS = 3
SCHEMA = {"type": "object", "properties": {"products": {"type": "array", "items": {"type": "object", "properties": {
    "name": {"type": "string"}, "price": {"type": "number"}, "size": {"type": "string"}}}}}}


def enabled() -> bool:
    return bool(get_settings().firecrawl_api_key)


def _scrape(url: str) -> list[dict]:
    r = httpx.post("https://api.firecrawl.dev/v2/scrape", timeout=120,
                   headers={"Authorization": f"Bearer {get_settings().firecrawl_api_key}"},
                   json={"url": url, "onlyMainContent": True, "formats": [
                       {"type": "json", "prompt": "List the grocery products shown with current price and size",
                        "schema": SCHEMA}]})
    r.raise_for_status()
    return ((r.json().get("data") or {}).get("json") or {}).get("products") or []


def search(s, query: str) -> list[dict]:
    """Candidates in the same shape as flipp.search, from store websites. [] when off or over today's cap."""
    q = " ".join(query.lower().split())[:60]
    key = f"web:{q}"
    if (hit := cache_get(s, key)) is not None:
        return hit["items"]
    if not enabled():
        return []
    since = utcnow() - timedelta(days=1)
    used = s.scalar(select(func.count(Event.id)).where(Event.kind == "firecrawl", Event.created_at > since))
    if used + len(SITES) > get_settings().daily_firecrawl_pages:
        log.info("firecrawl daily cap reached")
        return []
    until, items = (utcnow() + timedelta(days=CACHE_DAYS)).isoformat(), []
    for store, url in SITES.items():
        s.add(Event(kind="firecrawl", data={"store": store, "q": q}))
        try:
            products = _scrape(url.format(q=quote(q)))
        except httpx.HTTPError as e:
            log.warning("firecrawl %s failed: %s", store, type(e).__name__)
            continue
        for p in products[:20]:
            try:
                price = float(p.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 0 or not p.get("name"):
                continue
            name = f"{p['name']} {p.get('size') or ''}".strip()[:120]
            iid = "w" + hashlib.sha1(f"{store}:{name}:{price}".encode()).hexdigest()[:12]
            items.append({"id": iid, "store": store, "name": name, "price": f"{price:.2f}", "was": "", "unit": "",
                          "kind": "store site", "source": "website", "link": url.format(q=quote(q)),
                          "valid_from": None, "valid_to": until})
    cache_put(s, key, {"items": items}, timedelta(days=CACHE_DAYS))
    log.info("website prices for %r: %d items", q, len(items))
    return items
