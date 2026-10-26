"""Where the customer is and which grocery stores are near them.

Postal areas come from bundled GeoNames data (CC BY 4.0); stores from OpenStreetMap via the public Overpass
API (ODbL, (c) OpenStreetMap contributors), cached per postal area so each area is fetched about once a week.
"""
import csv
import logging
import math
import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import httpx

from app.models import CacheEntry
from app.util import utcnow

log = logging.getLogger("dh.places")
UA = "DollarHunter/0.1 (+https://dh.thoufeekxlabs.com)"
OVERPASS = "https://overpass-api.de/api/interpreter"
FSA = re.compile(r"^\s*([A-Za-z]\d[A-Za-z])\s*(\d[A-Za-z]\d)?\s*$")
STORE_DAYS = 7


@lru_cache
def _areas() -> dict:
    path = Path(__file__).resolve().parent.parent / "geo" / "ca_fsa.tsv"
    with path.open(encoding="utf-8") as f:
        rows = csv.DictReader((line for line in f if not line.startswith("#")), delimiter="\t")
        return {r["fsa"]: {"fsa": r["fsa"], "place": r["place"], "province": r["province"],
                           "lat": float(r["lat"]), "lon": float(r["lon"])} for r in rows}


def lookup_postal(text: str) -> dict | None:
    """'K2C 0C1' or 'k2c' -> {fsa, place, province, lat, lon, postal}; None if not a known Canadian area."""
    m = FSA.match(text or "")
    if not m:
        return None
    area = _areas().get(m.group(1).upper())
    if not area:
        return None
    return {**area, "postal": (m.group(1) + (m.group(2) or "")).upper()}


def cache_get(s, key: str):
    e = s.get(CacheEntry, key)
    return e.data if e and e.expires_at > utcnow() else None


def cache_put(s, key: str, data, ttl: timedelta):
    e = s.get(CacheEntry, key)
    if e:
        e.data, e.expires_at = data, utcnow() + ttl
    else:
        s.add(CacheEntry(key=key, data=data, expires_at=utcnow() + ttl))
    s.flush()


def _km(a_lat, a_lon, b_lat, b_lon) -> float:
    dlat, dlon = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def nearby_stores(s, area: dict, radius_km: float = 5) -> list[dict]:
    """Supermarkets near the postal area, closest first: [{name, address, km}]."""
    key = f"stores:{area['fsa']}:{radius_km:g}"
    if (hit := cache_get(s, key)) is not None:
        return hit["stores"]
    q = (f'[out:json][timeout:25];nwr[shop~"^(supermarket|wholesale)$"]'
         f'(around:{int(radius_km * 1000)},{area["lat"]},{area["lon"]});out center tags;')
    r = httpx.post(OVERPASS, data={"data": q}, headers={"User-Agent": UA}, timeout=40)
    r.raise_for_status()
    out = []
    for e in r.json().get("elements", []):
        t, lat, lon = e.get("tags", {}), e.get("lat") or e.get("center", {}).get("lat"), \
            e.get("lon") or e.get("center", {}).get("lon")
        name = t.get("brand") or t.get("name")
        if not name or lat is None:
            continue
        addr = " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street")]))
        out.append({"name": name, "address": addr, "km": round(_km(area["lat"], area["lon"], lat, lon), 1)})
    out.sort(key=lambda x: x["km"])
    cache_put(s, key, {"stores": out}, timedelta(days=STORE_DAYS))
    log.info("fetched %d stores near %s", len(out), area["fsa"])
    return out
