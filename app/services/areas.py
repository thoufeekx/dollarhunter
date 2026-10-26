import logging

from sqlalchemy import select

from app.models import Customer
from app.providers import catalog

log = logging.getLogger("dh.areas")


def refresh_active_areas(session_factory) -> int:
    """Re-download flyers (and store addresses) for every postal area with customers when older than 12 hours.
    Each area downloads in its own background thread, so customer messages are never held up."""
    with session_factory() as s:
        areas = {(c.preferences or {})["location"]["fsa"]: (c.preferences or {})["location"]["postal"]
                 for c in s.scalars(select(Customer).where(Customer.postal_prefix.is_not(None)))
                 if (c.preferences or {}).get("location")}
        stale = [postal for fsa, postal in areas.items() if not catalog.fresh(s, fsa)]
    for postal in stale:
        catalog.refresh_in_background(postal)
    return len(stale)
