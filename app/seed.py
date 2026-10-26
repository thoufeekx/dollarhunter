import sys

from sqlalchemy import select

from app.db import session_scope
from app.models import Customer
from app.providers.prices import ensure_fixture_offers

DEMO = [("+14165550101", "Demo User One", "L3T"), ("+14165550102", "Demo User Two", "L4J")]


def seed(reset=False):
    with session_scope() as s:
        for phone, name, prefix in DEMO:
            c = s.execute(select(Customer).where(Customer.phone == phone)).scalar_one_or_none()
            if c is None:
                s.add(Customer(phone=phone, display_name=name, postal_prefix=prefix, is_demo=True, onboarding_state="ready",
                               sms_consent_state="granted", sms_consent_source="seed", preferences={"substitutions": "ask"}))
        ensure_fixture_offers(s)
    print("seeded demo customers and fixture offers")


if __name__ == "__main__":
    seed("--reset" in sys.argv)
