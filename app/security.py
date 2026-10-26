import hmac
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException

from app.config import get_settings


def require_admin(x_admin_token: str | None = Header(default=None)):
    tok = get_settings().admin_token
    if not tok or not x_admin_token or not hmac.compare_digest(tok, x_admin_token):
        raise HTTPException(404)


def require_demo(x_admin_token: str | None = Header(default=None)):
    if not get_settings().demo_mode:
        raise HTTPException(404)
    require_admin(x_admin_token)


class RateLimiter:
    def __init__(self, limit: int, window: int):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def check(self, key: str):
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(429, "Too many requests")
        q.append(now)
