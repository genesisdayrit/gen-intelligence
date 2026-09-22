"""Redis SET NX lock with token-safe release.

Used to serialize Dropbox mutates for the same Knowledge Hub / journal
target without a worker pool. Redis errors fail open so a brief outage
does not drop a write. A crashed holder cannot wedge forever: the key
expires after ``ttl_seconds``.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from config import redis_client

logger = logging.getLogger(__name__)

# Long enough for a Reader KH create (web fetch + Dropbox + Raindrop).
# Short enough that a crashed holder unblocks the next webhook.
DEFAULT_LOCK_TTL_SECONDS = 60
# Wait up to most of one TTL for a live holder, then proceed (fail-open)
# so a slow/wedged writer cannot drop later events indefinitely.
DEFAULT_LOCK_WAIT_SECONDS = 45
DEFAULT_LOCK_POLL_SECONDS = 0.1

_RELEASE_IF_OWNER = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def _acquire(client, key: str, token: str, ttl_seconds: int) -> bool:
    return bool(client.set(key, token, nx=True, ex=int(ttl_seconds)))


def _release(client, key: str, token: str) -> None:
    client.eval(_RELEASE_IF_OWNER, 1, key, token)


@contextmanager
def redis_lock(
    key: str,
    *,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    poll_interval: float = DEFAULT_LOCK_POLL_SECONDS,
    client=None,
    fail_open: bool = True,
) -> Iterator[bool]:
    """Acquire ``key`` (SET NX + TTL). Always release in ``finally`` if held.

    Yields True when this caller owns the lock. Yields False when Redis
    failed or the wait expired — the body still runs so a write is not
    dropped. ``fail_open=False`` re-raises Redis errors; a wait timeout
    still runs the body and yields False.

    Release compares the owner token so an expired lock that another
    caller now holds is not deleted.
    """
    redis = client if client is not None else redis_client
    token = uuid.uuid4().hex
    acquired = False
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    try:
        while True:
            try:
                if _acquire(redis, key, token, ttl_seconds):
                    acquired = True
                    break
            except Exception:
                logger.exception(
                    "Redis lock acquire failed key=%s; %s",
                    key,
                    "proceeding without lock" if fail_open else "raising",
                )
                if fail_open:
                    acquired = False
                    break
                raise
            if time.monotonic() >= deadline:
                logger.warning(
                    "Redis lock wait expired key=%s ttl=%ss wait=%ss; proceeding without lock",
                    key,
                    ttl_seconds,
                    wait_seconds,
                )
                if not fail_open:
                    yield False
                    return
                break
            time.sleep(max(0.01, float(poll_interval)))
        yield acquired
    finally:
        if acquired:
            try:
                _release(redis, key, token)
            except Exception:
                logger.exception("Redis lock release failed key=%s", key)
