#!/usr/bin/env python3
"""Write .env.server (mode 600) for the server deploy, starting from the local .env.

    scripts/make_server_env.py https://dh.example.com

Keeps the admin token and database password from an existing .env.server so re-running is safe.
Never prints secret values.
"""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DROP = {"DH_DATABASE_URL", "DH_STORAGE_DIR"}  # set by the server compose file


def read(p: Path) -> dict:
    out = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v
    return out


def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("usage: scripts/make_server_env.py https://your.domain")
    url = sys.argv[1].rstrip("/")
    local, old = read(ROOT / ".env"), read(ROOT / ".env.server")
    env = {k: v for k, v in local.items() if k not in DROP}
    env.update({
        "DH_ENV": "production",
        "DH_PUBLIC_BASE_URL": url,
        "DH_ADMIN_TOKEN": old.get("DH_ADMIN_TOKEN") or secrets.token_urlsafe(32),
        "DH_DB_PASSWORD": old.get("DH_DB_PASSWORD") or secrets.token_urlsafe(24),
        # deploy-only settings, kept from the previous file (set them once by hand)
        **{k: old[k] for k in ("DH_DEPLOY_HOST", "DH_TUNNEL_ID", "DH_PROTECTED_FILES") if k in old},
        "DH_SMS_PROVIDER": "twilio",
        "DH_STORAGE_BACKEND": "r2",
        "DH_PAYMENTS_ENABLED": "false",
        "DH_REAL_ORDERS_ENABLED": "false",
    })
    path = ROOT / ".env.server"
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    path.chmod(0o600)
    print(f"wrote {path.name}: {len(env)} settings, public URL {url}")


if __name__ == "__main__":
    main()
