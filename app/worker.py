import logging
import signal
import time

from app.config import get_settings
from app.db import session_factory
from app.providers.sms import ConsoleSms, get_sms_provider
from app.services.inbound import handle_process_inbound
from app.services.receipts import handle_process_receipt, purge_expired_files
from app.services.retention import prune_old_rows
from app.services.areas import refresh_active_areas
from app.services.jobs import claim_job, finish_job
from app.services.outbox import send_pending
from app.models import Job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("dh.worker")

def handle_refresh_area(s, payload: dict):
    from app.providers import catalog
    if not catalog.fresh(s, payload["postal"][:3]):
        catalog.refresh_area(s, payload["postal"])


HANDLERS = {"process_inbound": handle_process_inbound, "process_receipt": handle_process_receipt,
            "refresh_area": handle_refresh_area}
PURGE_EVERY = 3600


def run_one(sf) -> bool:
    jid = claim_job(sf)
    if not jid:
        return False
    err = None
    try:
        with sf() as s:
            j = s.get(Job, jid)
            HANDLERS[j.kind](s, j.payload)
            s.commit()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
        log.warning("job %s failed: %s", jid, type(e).__name__)
    finish_job(sf, jid, err)
    return True


def main():
    s = get_settings()
    for w in s.validate_startup():
        log.warning(w)
    sf = session_factory()
    provider, demo = get_sms_provider(), ConsoleSms()
    stop, next_purge = {"v": False}, 0.0
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    log.info("worker started: sms=%s ai=%s", s.effective_sms_provider, s.effective_ai_provider)
    while not stop["v"]:
        busy = run_one(sf)
        busy = bool(send_pending(sf, provider, demo_provider=demo)) or busy
        if time.monotonic() >= next_purge:
            try:
                purge_expired_files(sf)
                prune_old_rows(sf)
                refresh_active_areas(sf)
            except Exception as e:
                log.warning("purge failed: %s", type(e).__name__)
            next_purge = time.monotonic() + PURGE_EVERY
        if not busy:
            time.sleep(1)


if __name__ == "__main__":
    main()
