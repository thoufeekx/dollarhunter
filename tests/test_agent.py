"""Agent behaviour with a scripted model, a rule-based stand-in matcher and fake flyer data (no network)."""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

import app.agent as agent
from app import comparison, matcher
from app.models import BasketLine, Customer, Message, Quote
from app.util import utcnow
from tests.test_core import A, drain, inbound

LATER = (utcnow() + timedelta(days=3)).isoformat()


def item(iid, store, name, price, was="", unit="", kind="flyer"):
    return {"id": iid, "store": store, "name": name, "price": price, "was": was, "unit": unit, "kind": kind,
            "valid_from": None, "valid_to": LATER}


CATALOG = [item("t1", "FreshCo", "Colgate Toothpaste 95 mL", "1.29", "1.99"),
           item("t2", "Walmart", "Crest Toothpaste 100 mL", "1.50"),
           item("m1", "Walmart", "Natrel 2% Milk 2L", "3.00", "6.00"),
           item("m2", "FreshCo", "Beatrice 2% Milk 2L", "3.20", "6.40"),
           item("a1", "FreshCo", "Gala Apples 3 lb bag", "2.99", "4.99"),
           item("a2", "Walmart", "Apple Chips 100g", "0.99"),
           item("b1", "FreshCo", "Toothbrush Oral-B", "0.50")]


def call(tool, **args):
    return {"id": f"c-{tool}", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}


def fake_match(want, cand):
    w = want.split()[0].rstrip("s")
    return w in cand["name"].lower() and "chips" not in cand["name"].lower()


@pytest.fixture
def scripted(monkeypatch):
    script, seen = [], []

    def chat(msgs, tools, json_mode=False, model=None):
        seen.append(msgs)
        step = script.pop(0)
        return step(msgs) if callable(step) else step

    monkeypatch.setattr(agent, "enabled", lambda: True)
    monkeypatch.setattr(agent, "chat", chat)
    monkeypatch.setattr(agent.flipp, "search", lambda s, postal, q, merchants=None: list(CATALOG))
    monkeypatch.setattr(agent.catalog, "fresh", lambda s, fsa: True)
    monkeypatch.setattr(agent.catalog, "search", lambda s, fsa, words, limit=60: list(CATALOG))

    def jev(reqs, s=None, context=""):  # stand-in for the Jev decision model: best match per store
        out = {}
        for r in reqs:
            for c in sorted((c for c in r["candidates"] if fake_match(r["want"], c)), key=lambda c: Decimal(c["price"])):
                if c["store"] not in {x["store"] for x in out.get(r["line_id"], [])}:
                    out.setdefault(r["line_id"], []).append(c)
        return out
    monkeypatch.setattr(matcher, "jev_best_per_store", jev)
    monkeypatch.setattr(agent, "nearby_stores", lambda s, area, radius_km=5: [{"name": "FreshCo", "address": "1 Main", "km": 0.4}])
    monkeypatch.setattr(matcher, "match_all", lambda reqs: {r["line_id"]: [c["id"] for c in r["candidates"]
                                                                        if fake_match(r["want"], c)] for r in reqs})
    monkeypatch.setattr(matcher, "rank_options", lambda want, size, cands, n=3: [c["id"] for c in sorted(
        (c for c in cands if fake_match(want, c)), key=lambda c: Decimal(c["price"]))][:n])
    return script, seen


def outs(sf):
    with sf() as s:
        return [m.body for m in s.scalars(select(Message).order_by(Message.created_at))]


def located(sf, phone=A):
    with sf() as s:
        s.add(Customer(phone=phone, is_demo=True, onboarding_state="ready", postal_prefix="K2C", preferences={
            "location": {"fsa": "K2C", "postal": "K2C0C1", "place": "Ottawa", "province": "ON", "lat": 45.3, "lon": -75.7}}))
        s.commit()


def test_location_from_conversation(sf, scripted):
    script, seen = scripted
    script += [{"tool_calls": [call("set_location", postal_code="K2C 0C1"), call("find_stores")]},
               {"content": "Got it, Ottawa. Near you: FreshCo. What do you need?"}]
    inbound(sf, A, "I'm at K2C 0C1", "g1"); drain(sf)
    with sf() as s:
        c = s.scalar(select(Customer))
        assert c.postal_prefix == "K2C" and c.preferences["location"]["place"].startswith("Ottawa")
    assert outs(sf)[-1].startswith("Got it, Ottawa")


def test_correction_changes_the_item_instead_of_adding(sf, scripted):
    script, _ = scripted
    located(sf)
    script += [{"tool_calls": [call("update_basket", add=[{"name": "apples"}])]}, {"content": "Added 1 apples."},
               {"tool_calls": [call("update_basket", change=[{"name": "apples", "size": "bag"}])]}, {"content": "A bag it is."}]
    inbound(sf, A, "some apples", "c1"); drain(sf)
    inbound(sf, A, "not one apple, a bag", "c2"); drain(sf)
    with sf() as s:
        lines = s.scalars(select(BasketLine)).all()
    assert [(l.name, l.size_pref, l.quantity) for l in lines] == [("apples", "bag", Decimal("1.000"))]


def test_price_basket_compares_stores_and_texts_quote(sf, scripted):
    script, seen = scripted
    located(sf)
    script += [{"tool_calls": [call("update_basket", add=[{"name": "toothpaste", "quantity": 2},
                                                        {"name": "milk", "size": "2L"}])]}, {"content": "Added."},
               {"tool_calls": [call("price_basket")]}]
    inbound(sf, A, "2 toothpaste and 2L milk", "p1"); drain(sf)
    inbound(sf, A, "find me the cheapest, give me the code", "p2"); drain(sf)
    body = outs(sf)[-1]
    # FreshCo has both (2x1.29 + 3.20 = 5.78) vs mix (2x1.29 FreshCo + 3.00 Walmart = 5.58): within $2, one store wins
    assert body.startswith("Best: all at FreshCo. Total $6.93") and "FreshCo: milk (Beatrice) $3.20" in body
    assert "2 toothpaste (Colgate) $2.58" in body and "You save $4.60 vs regular $10.38" in body and "demo order" not in body
    with sf() as s:
        assert s.scalar(select(Quote)).total == Decimal("6.93")  # 5.78 + 25% of 4.60


def test_options_then_choose_pins_that_product(sf, scripted):
    script, _ = scripted
    located(sf)
    script += [{"tool_calls": [call("show_options", name="toothpaste")]}]
    inbound(sf, A, "cheapest toothpaste options?", "o1"); drain(sf)
    body = outs(sf)[-1]
    assert body.startswith("Cheapest toothpaste near you: 1) Colgate Toothpaste 95 mL $1.29 @FreshCo, was $1.99; "
                           "2) Crest Toothpaste 100 mL $1.50 @Walmart") and "Toothbrush" not in body
    script += [{"tool_calls": [call("choose_option", number=2)]}, {"content": "Added the Crest."},
               {"tool_calls": [call("price_basket")]}]
    inbound(sf, A, "2", "o2"); drain(sf)
    inbound(sf, A, "total?", "o3"); drain(sf)
    assert "Walmart: toothpaste (Crest) $1.50" in outs(sf)[-1]


def test_memory_reaches_the_next_conversation(sf, scripted):
    script, seen = scripted
    located(sf)
    script += [{"tool_calls": [call("remember", fact="Always buys Heinz ketchup")]}, {"content": "Noted: Heinz."},
               {"content": "Hi again!"}]
    inbound(sf, A, "I always buy Heinz ketchup", "r1"); drain(sf)
    inbound(sf, A, "hey", "r2"); drain(sf)
    system = seen[-1][0]["content"]
    assert "always buys heinz ketchup" in system and any("Heinz" in m.get("content", "") for m in seen[-1][1:])


def test_model_typed_prices_are_sent_back(sf, scripted):
    script, _ = scripted
    located(sf)
    script += [{"content": "Toothpaste is $1.29 at FreshCo."}, {"content": "Want options for toothpaste?"}]
    inbound(sf, A, "how much is toothpaste", "x1"); drain(sf)
    assert outs(sf)[-1] == "Want options for toothpaste?"


def test_stop_and_approval_never_reach_the_model(sf, scripted):
    script, seen = scripted
    inbound(sf, A, "STOP", "s1"); drain(sf)
    inbound(sf, A, "START", "s2"); drain(sf)
    inbound(sf, A, "YES 123456", "s3"); drain(sf)
    assert seen == [] and "No open quote" in outs(sf)[-1]


def test_model_failure_never_turns_words_into_items(sf, scripted, monkeypatch):
    def boom(msgs, tools, json_mode=False, model=None):
        raise TimeoutError
    monkeypatch.setattr(agent, "chat", boom)
    located(sf)
    inbound(sf, A, "quote", "f1"); drain(sf)
    with sf() as s:
        assert s.scalar(select(BasketLine)) is None  # "quote" was not added as a grocery item
    assert "Something went wrong" in outs(sf)[-1]


def test_a_failing_tool_does_not_break_the_reply(sf, scripted, monkeypatch):
    script, _ = scripted
    located(sf)
    monkeypatch.setattr(agent.Tools, "find_stores", lambda self: 1 / 0)
    script += [{"tool_calls": [call("find_stores")]}, {"content": "Stores are slow right now. What do you need?"}]
    inbound(sf, A, "which stores?", "t1"); drain(sf)
    assert outs(sf)[-1] == "Stores are slow right now. What do you need?"


def test_daily_agent_limit(sf, scripted, monkeypatch):
    monkeypatch.setattr(agent.get_settings(), "daily_agent_turns", 0)
    located(sf)
    inbound(sf, A, "milk", "l1"); drain(sf)
    assert "message limit" in outs(sf)[-1]


def test_comparison_balances_price_and_stops():
    class L:
        def __init__(self, i, q):
            self.id, self.quantity = i, Decimal(q)
    lines = [L("x", 10), L("y", 1)]
    m = {"x": [item("1", "A", "apple", "0.50"), item("2", "B", "apple", "1.00")],
         "y": [item("3", "A", "milk", "6.00"), item("4", "B", "milk", "3.00")]}
    p = comparison.plan(lines, m, {})  # A only $11; A+B $8 + $3 for the extra stop = $11: tie goes to fewer stops
    assert p["label"] == "all at A" and any(c["kind"] == "cheapest anywhere" for c in p["compare"])
    assert comparison.plan(lines, m, {}, mode="cheapest")["label"] == "2 stores"
    assert comparison.plan(lines, m, {}, only_store="b")["label"] == "all at B"


def test_matcher_reads_nested_answers():
    assert [r["line_id"] for r in matcher._records([{"matches": [{"line_id": "L1", "ids": ["1"]}]}], "line_id")] == ["L1"]
    assert matcher._records({"result": {"ids": ["a"]}}, "ids") == [{"ids": ["a"]}]


def test_one_failed_search_does_not_sink_the_quote(sf, scripted, monkeypatch):
    import httpx
    script, _ = scripted
    located(sf)

    def flaky(s, postal, q, merchants=None):
        raise httpx.HTTPStatusError("422", request=httpx.Request("GET", "http://x"), response=httpx.Response(422))
    monkeypatch.setattr(agent.flipp, "search", flaky)  # live online search down
    monkeypatch.setattr(agent.catalog, "search", lambda s, fsa, words, limit=60: [i for i in CATALOG if "milk" not in words[0]])
    script += [{"tool_calls": [call("update_basket", add=[{"name": "toothpaste"}, {"name": "milk"}])]}, {"content": "ok"},
               {"tool_calls": [call("price_basket")]}]
    inbound(sf, A, "toothpaste and milk", "f1"); drain(sf)
    inbound(sf, A, "quote", "f2"); drain(sf)
    assert "toothpaste (Colgate) $1.29" in outs(sf)[-1] and "Not found: milk" in outs(sf)[-1]


def test_partial_postal_code_is_completed(monkeypatch):
    from app.providers import flipp
    seen = {}

    class R:
        def json(self): return {"items": [], "ecom_items": []}
    monkeypatch.setattr(flipp, "_get", lambda params: seen.update(params) or R())
    monkeypatch.setattr(flipp, "cache_get", lambda s, k: None)
    monkeypatch.setattr(flipp, "cache_put", lambda s, k, d, t: None)
    flipp.search(None, "k2c", "apples")
    assert seen["postal_code"] == "K2C1A1"


def test_english_side_of_bilingual_names():
    from app.util import english
    assert english("PILONS DE POULET | CHICKEN DRUMSTICKS") == "CHICKEN DRUMSTICKS"
    assert english("BEURRE SELECTION | SELECTION BUTTER") == "SELECTION BUTTER"
    assert english("Gay Lea butter") == "Gay Lea butter"


def test_trip_planner_avoids_store_hopping():
    class L:
        def __init__(self, i, q): self.id, self.quantity = i, Decimal(q)
    # 5 items: store A has all at slightly higher prices; the absolute cheapest needs 5 different stores
    lines = [L(str(n), 1) for n in range(5)]
    m = {str(n): [item(f"a{n}", "A", "x", "2.00"), item(f"s{n}", f"S{n}", "x", "1.70")] for n in range(5)}
    p = comparison.plan(lines, m, {})
    assert p["label"] == "all at A"  # $10.00 at one store beats $8.50 spread over 5 stops
    assert any(c["kind"] == "cheapest anywhere" and c["stops"] == 5 for c in p["compare"])


def test_price_guard_allows_the_customers_own_numbers():
    assert agent._stray_prices("Budget set to $80, want a quote?", {"$80"}) == []
    assert agent._stray_prices("Got it, $80.00 budget", {"$80"}) == []
    assert agent._stray_prices("Toothpaste is $1.29 at FreshCo", {"$80"}) == ["$1.29"]


def test_clear_keeps_previous_list_in_context(sf, scripted):
    script, seen = scripted
    located(sf)
    script += [{"tool_calls": [call("update_basket", add=[{"name": "bread", "keywords": ["bread"]}])]}, {"content": "ok"},
               {"tool_calls": [call("clear_basket")]}, {"content": "Cleared."}, {"content": "You had bread."}]
    inbound(sf, A, "bread", "k1"); drain(sf)
    inbound(sf, A, "start fresh", "k2"); drain(sf)
    inbound(sf, A, "what was on my old list?", "k3"); drain(sf)
    assert "Previous list (cleared, can be added back on request): 1 bread" in seen[-1][0]["content"]
    with sf() as s:
        assert s.scalar(select(BasketLine)) is None
