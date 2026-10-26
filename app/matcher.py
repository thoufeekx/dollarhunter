"""Product matching: the one judgement call in pricing that needs a model.

Code fetches the candidates and computes every price; the model only answers "which of these real products is
what the customer asked for?" and returns item ids. Anything it returns that is not a candidate is ignored.
"""
import hashlib
import json
import logging
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from app import agent
import httpx

from app.config import get_settings
from app.providers.places import cache_get, cache_put

log = logging.getLogger("dh.matcher")
MIN_CONFIDENCE = 0.15

RULES = """You match grocery requests to real store products. A candidate matches when it is what this customer
most plausibly means, judging from their own words, any size or brand they gave, and the context provided.
Never match a different kind of product than they asked for. Return only JSON."""


def dedupe(items: list[dict]) -> list[dict]:
    """The same product often appears under several ids (flyer pages, online listings): keep one."""
    seen, out = set(), []
    for i in items:
        k = (i["store"].lower(), " ".join(i["name"].lower().split()), i["price"])
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


def _cands(items):
    return [{"id": i["id"], "store": i["store"], "name": i["name"], "price": i["price"], "unit": i["unit"],
             "type": i.get("kind", "flyer")} for i in items]


def match_all(requests: list[dict]) -> dict:
    """requests: [{line_id, want, size, brand, candidates}] -> {line_id: [ids of every truly matching product]}.
    One small model call per item, run in parallel: one big call gets cut off on long baskets."""
    todo = [r for r in requests if r["candidates"]]
    if not todo:
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
        found = list(pool.map(_match_one, todo))
    return {r["line_id"]: ids for r, ids in zip(todo, found)}


def _match_one(r: dict) -> list[str]:
    out = _json(RULES + '\nList EVERY candidate that genuinely matches the request (any store) as {"ids": [...]}; '
                '[] when none match.\n' + json.dumps({"want": r["want"], "size": r.get("size"), "brand": r.get("brand"),
                                                     "candidates": _cands(r["candidates"])}))
    ok = {i["id"] for i in r["candidates"]}
    ids = next((x["ids"] for x in _records(out, "ids")), out if isinstance(out, list) else [])
    return [str(i) for i in ids if str(i) in ok]


def rank_options(want: str, size: str | None, candidates: list[dict], n: int = 3) -> list[str]:
    """Up to n candidate ids that really match `want`, cheapest first."""
    if not candidates:
        return []
    out = _json(RULES + f'\nList up to {n} matching candidates, cheapest first, as {{"ids": [...]}}.\n' +
                json.dumps({"want": want, "size": size, "candidates": _cands(candidates)}))
    ok = {i["id"] for i in candidates}
    ids = next((r["ids"] for r in _records(out, "ids")), out if isinstance(out, list) else [])
    return [str(i) for i in ids if str(i) in ok][:n]


def _records(data, key: str) -> list[dict]:
    """Every dict carrying `key`, however deeply the model wrapped it."""
    if isinstance(data, dict):
        return [data] if key in data else [r for v in data.values() for r in _records(v, key)]
    if isinstance(data, list):
        return [r for v in data for r in _records(v, key)]
    return []


def _json(prompt: str) -> dict | list:
    m = agent.chat([{"role": "user", "content": prompt}], None, json_mode=True, model=get_settings().match_model)
    try:
        return json.loads((m.get("content") or "{}").strip().removeprefix("```json").removesuffix("```"))
    except ValueError:
        log.warning("matcher returned non-JSON")
        return {}


JEV_STATE = ("Canadian grocery shopping. For each request, pick the store product this customer most plausibly "
             "means, judging from their own words and the context given (their saved preferences and notes for this "
             "basket). Never pick a different kind of product than they asked for. If nothing fits, answer none.")


def jev_best_per_store(requests: list[dict], s=None, context: str = "") -> dict:
    """One Jev decision request for the whole basket: for every item and store, the matching product or none.
    requests: [{line_id, want, size, brand, candidates}] -> {line_id: [best candidate at each store]}.
    With a session, decisions are cached per item and candidate set, so the same basket always gets the same
    picks (and repeat quotes skip the model call)."""
    out, todo = {}, []
    for r in requests:
        key = _decision_key(r)
        hit = cache_get(s, key) if s is not None else None
        if hit is not None:
            ids = set(hit["ids"])
            out[r["line_id"]] = [c for c in r["candidates"] if c["id"] in ids]
        else:
            todo.append((r, key))
    if todo:
        fresh = _jev([r for r, _ in todo], context)
        for r, key in todo:
            out[r["line_id"]] = fresh.get(r["line_id"], [])
            if s is not None:
                cache_put(s, key, {"ids": [c["id"] for c in out[r["line_id"]]]}, timedelta(hours=6))
    return {k: v for k, v in out.items() if v}


def _decision_key(r: dict) -> str:
    raw = json.dumps([r["want"], r.get("size"), r.get("brand"), r.get("context", ""),
                      sorted(c["id"] for c in r["candidates"])])
    return "jev:" + hashlib.sha1(raw.encode()).hexdigest()


def _jev(requests: list[dict], context: str = "") -> dict:
    qs, index = {}, {}
    for r in requests:
        by_store = {}
        for c in r["candidates"]:
            by_store.setdefault(c["store"], []).append(c)
        for n, (store, cands) in enumerate(by_store.items()):
            key = f"q{len(qs)}"
            crit = {f"o{k}": f"{c['name']} | ${c['price']}{' ' + c['unit'] if c.get('unit') else ''}"
                    for k, c in enumerate(cands[:8])}
            crit["none"] = "none of these is what the customer asked for"
            want = r["want"] + "".join(f", {x}" for x in (r.get("size"), r.get("brand")) if x)
            qs[key] = {"type": "choice", "instructions": f"The customer wants: {want}. Which product is it?",
                       "criteria": crit}
            index[key] = (r["line_id"], cands[:8])
    if not qs:
        return {}
    cfg = get_settings()
    for wait in (0.5, None):
        resp = httpx.post(f"{cfg.ai_base_url.rstrip('/')}/../alpha/decisions".replace("/v1/../", "/"), timeout=30,
                          headers={"Authorization": f"Bearer {cfg.ai_api_key}"},
                          json={"model": cfg.decision_model, "state": [JEV_STATE, *([context] if context else [])],
                                "questions": qs})
        if resp.status_code in (429, 500, 502, 503, 529) and wait:
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    out = {}
    for key, ans in (resp.json().get("answers") or {}).items():
        if key not in index or ans.get("choice") in (None, "none") or (ans.get("confidence") or 0) < MIN_CONFIDENCE:
            continue
        lid, cands = index[key]
        k = int(str(ans["choice"])[1:])
        if k < len(cands):
            out.setdefault(lid, []).append(cands[k])
    return out
