from decimal import Decimal

from sqlalchemy import select

from app.catalog import STAPLES
from app.config import get_settings
from app.models import Basket, BasketLine, Customer
from app.providers.base import ParsedList


def open_basket(s, customer: Customer) -> Basket:
    b = s.execute(select(Basket).where(Basket.customer_id == customer.id,
                                       Basket.status.in_(["draft", "confirmed", "quoted"]))).scalar_one_or_none()
    if b:
        return b
    b = Basket(customer_id=customer.id, budget=get_settings().default_budget_cad, instructions=[])
    s.add(b)
    s.flush()
    return b


def lines_of(s, basket: Basket, suggested: bool | None = False):
    """Basket lines. suggested=False: the wanted items; True: receipt suggestions only; None: both."""
    q = select(BasketLine).where(BasketLine.basket_id == basket.id, BasketLine.customer_id == basket.customer_id)
    if suggested is not None:
        q = q.where((BasketLine.status == "suggested") if suggested else (BasketLine.status != "suggested"))
    return s.execute(q.order_by(BasketLine.name)).scalars().all()


def missing_quantity(s, basket: Basket) -> list[str]:
    return [l.name for l in lines_of(s, basket) if l.quantity is None]


def apply_parsed(s, basket: Basket, parsed: ParsedList, source: str, source_ref: str | None,
                 suggest: bool = False) -> list[str]:
    """Merge parsed items into the basket. suggest=True (receipts) only proposes lines for the user to pick."""
    questions = []
    current = lines_of(s, basket, suggested=None)
    existing = {(l.sku or l.name): l for l in current}
    by_name = {l.name: l for l in current}
    for rm in parsed.removals:
        for k, l in list(existing.items()):
            if rm in l.name or l.name in rm:
                s.delete(l)
                existing.pop(k)
    excluded = [i.text for i in parsed.instructions if i.type == "exclude"]
    for it in parsed.items:
        name = STAPLES[it.sku]["name"] if it.sku else it.name
        if any(x in name or name in x for x in excluded):
            continue
        key = it.sku or name
        unit = STAPLES[it.sku]["unit"] if it.sku else it.unit
        if it.question:
            questions.append(it.question)
            continue
        if key not in existing and name in by_name:
            key = by_name[name].sku or name  # same item recorded under its catalogue code
        if key in existing:
            l = existing[key]
            if suggest:
                continue  # a receipt never overrides what the user already asked for
            if it.quantity is not None:
                l.quantity = it.quantity
            if l.status == "suggested" or it.quantity is not None:
                l.status = "proposed" if l.quantity is not None else "needs_quantity"
            continue
        status = "suggested" if suggest else ("proposed" if it.quantity is not None else "needs_quantity")
        s.add(BasketLine(customer_id=basket.customer_id, basket_id=basket.id, name=name, sku=it.sku,
                         quantity=it.quantity, unit=unit, source=source, source_ref=source_ref,
                         confidence=Decimal(str(it.confidence)), status=status))
    known = {(i["type"], i["text"]) for i in basket.instructions}
    new = [i.model_dump() for i in parsed.instructions if (i.type, i.text) not in known]
    if new:
        basket.instructions = [*basket.instructions, *new]
    touch(basket)
    s.flush()
    return questions


def touch(basket: Basket):
    """Record an edit: any quote for the previous version is no longer approvable."""
    basket.version += 1
    if basket.status == "quoted":
        basket.status = "draft"


def clear(s, basket: Basket):
    for l in lines_of(s, basket, suggested=None):
        s.delete(l)
    basket.instructions = []
    touch(basket)
    s.flush()


def get_line(s, basket: Basket, line_id) -> BasketLine | None:
    return s.execute(select(BasketLine).where(BasketLine.id == line_id, BasketLine.basket_id == basket.id,
                                              BasketLine.customer_id == basket.customer_id)).scalar_one_or_none()


def set_quantity(s, basket: Basket, line: BasketLine, qty: Decimal | None):
    if qty is not None and qty <= 0:
        s.delete(line)
    else:
        line.quantity = qty
        line.status = "proposed" if qty is not None else "needs_quantity"  # also accepts a receipt suggestion
    touch(basket)
    s.flush()


def set_budget(s, basket: Basket, amount: Decimal):
    basket.budget = amount
    touch(basket)
    s.flush()


def summary(s, basket: Basket) -> str:
    parts = []
    for l in lines_of(s, basket):
        q = f"{l.quantity.normalize():f} " if l.quantity is not None else ""
        parts.append(f"{q}{l.name}" + ("" if l.quantity is not None else " (qty?)"))
    msg = "Draft basket: " + ("; ".join(parts) if parts else "empty") + "."
    ex = [i["text"] for i in basket.instructions if i["type"] == "exclude"]
    if ex:
        msg += " This basket only: no " + ", no ".join(ex) + "."
    return msg
