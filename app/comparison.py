"""Shopping trip planning: which 1-3 stores to buy the basket at, and what each choice costs.

Pure arithmetic over matched products; no model involved. Every combination of up to MAX_STOPS of the
best-covering stores is tried; each item is bought at the cheapest store in the combination. The pick
minimises price plus TRIP_COST per extra stop, and the quote also shows the absolute cheapest spread.
"""
from decimal import Decimal
from itertools import combinations

TRIP_COST = Decimal("3.00")  # what one more store stop is worth to a shopper
MAX_STOPS = 3
POOL = 8                     # stores considered for combinations (by coverage, then price)


def _price(o) -> Decimal:
    return o.price if hasattr(o, "price") else Decimal(o["price"])


def _store(o) -> str:
    return o.store if hasattr(o, "store") else o["store"]


def plan(lines, matches: dict, pinned: dict, only_store: str | None = None, mode: str | None = None) -> dict:
    """lines: basket lines; matches: {line_id: [candidate per store]}; pinned: {line_id: Offer} the user chose.
    mode: None (balanced), "cheapest" (any number of stores) or "fewest" (fewest stores).
    Returns {picks: {line_id: candidate or Offer}, compare: [...], label}."""
    qty = {str(l.id): l.quantity for l in lines}
    by_store: dict[str, dict] = {}
    for lid, cands in matches.items():
        for c in cands:
            cur = by_store.setdefault(_store(c), {}).get(lid)
            if cur is None or _price(c) < _price(cur):
                by_store[_store(c)][lid] = c
    wanted = [lid for lid in qty if lid in matches and matches[lid] and lid not in pinned]

    def cost(picks):
        return sum((_price(o) * qty[lid] for lid, o in picks.items()), Decimal(0))

    def stops(picks):
        return len({_store(o) for o in picks.values()})

    cheapest = dict(pinned)
    for lid in wanted:
        cheapest[lid] = min(matches[lid], key=_price)

    def for_stores(names):
        picks = dict(pinned)
        for lid in wanted:
            here = [by_store[n][lid] for n in names if lid in by_store[n]]
            picks[lid] = min(here, key=_price) if here else cheapest[lid]  # outside the trip: cheapest anywhere
        return picks

    ranked = sorted(by_store, key=lambda n: (-len(by_store[n]), cost(by_store[n])))[:POOL]
    options = [for_stores(c) for k in range(1, MAX_STOPS + 1) for c in combinations(ranked, k)] or [cheapest]
    options.append(cheapest)
    one_store = [o for o in options if stops({k: v for k, v in o.items() if k not in pinned}) == 1
                 and all(lid in o for lid in wanted)]

    if only_store:
        want = only_store.strip().lower()
        hit = next((n for n in by_store if want in n.lower()), None)
        if hit:
            options = [for_stores([hit])]
    score = {"cheapest": lambda o: (cost(o), stops(o)), "fewest": lambda o: (stops(o), cost(o))}.get(
        mode, lambda o: (cost(o) + TRIP_COST * (stops(o) - 1), stops(o)))
    best = min(options, key=score)

    compare = []
    best_single = min((o for o in one_store if all(by_store.get(_store(next(iter(o.values()))), {}).get(lid)
                                                     for lid in wanted)), key=cost, default=None)
    if best_single and best_single is not best:
        compare.append({"kind": "one store", "store": _store(next(iter(best_single.values()))),
                        "total": str(cost(best_single).quantize(Decimal("0.01")))})
    if stops(cheapest) > stops(best) and cost(cheapest) < cost(best):
        compare.append({"kind": "cheapest anywhere", "stops": stops(cheapest),
                        "total": str(cost(cheapest).quantize(Decimal("0.01")))})
    if stops(best) > 1 and best is not min(options, key=lambda o: (stops(o), cost(o))):
        fewest = min(options, key=lambda o: (stops(o), cost(o)))
        compare.append({"kind": "fewest stores", "stops": stops(fewest),
                        "total": str(cost(fewest).quantize(Decimal("0.01")))})
    names = sorted({_store(o) for o in best.values()})
    label = f"all at {names[0]}" if len(names) == 1 else f"{len(names)} stores"
    return {"picks": best, "compare": compare, "label": label}
