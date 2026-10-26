from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.catalog import FIXTURE_PRICES, STAPLES, STORE
from app.models import Offer
from app.util import utcnow

FIXTURE_DAYS = 7


def ensure_fixture_offers(s):
    """Insert a fresh week of labelled fixture offers when none are valid."""
    now = utcnow()
    if s.execute(select(Offer.id).where(Offer.is_fixture.is_(True), Offer.valid_until > now).limit(1)).first():
        return
    start, end = now - timedelta(days=1), now + timedelta(days=FIXTURE_DAYS)
    for sku, (product, brand, pack, regular, sale) in FIXTURE_PRICES.items():
        for kind, price in (("regular", regular), ("offer", sale)):
            if price is None:
                continue
            s.add(Offer(store=STORE, sku=sku, product=product, brand=brand, unit=STAPLES[sku]["unit"], pack_size=pack,
                        equivalence_key=sku, price=Decimal(price), kind=kind, valid_from=start, valid_until=end,
                        source_type="fixture", source_ref="app.catalog.FIXTURE_PRICES", fetched_at=now, is_fixture=True))
    s.flush()


class FixturePriceSource:
    def offers_for(self, s, sku: str) -> list[Offer]:
        now = utcnow()
        return s.execute(select(Offer).where(Offer.sku == sku, Offer.valid_from <= now, Offer.valid_until > now,
                                             Offer.availability == "in_stock").order_by(Offer.price)).scalars().all()


def get_price_source():
    return FixturePriceSource()
