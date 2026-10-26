"""Store a gzipped database dump read from stdin and keep only the newest KEEP backups.

    docker compose exec -T db pg_dump -U dollarhunter dollarhunter | gzip | docker compose exec -T worker python -m app.backup
"""
import logging
import sys

from app.config import get_settings
from app.storage import LocalStorage, R2Storage
from app.util import utcnow

log = logging.getLogger("dh.backup")
PREFIX, KEEP = "backups/", 7


def main():
    data = sys.stdin.buffer.read()
    if len(data) < 100:
        sys.exit("backup input is empty; refusing to store it")
    s = get_settings()
    key = f"{PREFIX}dollarhunter-{utcnow():%Y%m%dT%H%M%SZ}.sql.gz"
    if s.storage_backend == "r2":
        st = R2Storage(s)
        st.put(key, data)
        objs = st.c.list_objects_v2(Bucket=st.bucket, Prefix=PREFIX).get("Contents", [])
        old = sorted(o["Key"] for o in objs)[:-KEEP]
    else:
        st = LocalStorage(s.storage_dir)
        st.put(key, data)
        old = sorted(f"{PREFIX}{p.name}" for p in (st.root / PREFIX).glob("*.sql.gz"))[:-KEEP]
    for k in old:
        st.delete(k)
    print(f"stored {key} ({len(data)} bytes), removed {len(old)} old backups")


if __name__ == "__main__":
    main()
