"""Shared Dropbox client that lets the SDK refresh short-lived access tokens.

Hub scripts used to mint an access token, cache it in Redis with a TTL equal
to ``expires_in``, then construct ``dropbox.Dropbox(oauth2_access_token=...)``.
That client cannot refresh, so scheduled jobs fail with
``AuthError('expired_access_token')`` when Redis still holds a token Dropbox
has already expired (or that expires mid-request).

``dropbox.Dropbox(oauth2_refresh_token=..., app_key=..., app_secret=...)``
refreshes on 401. Env var names stay the same:
``DROPBOX_ACCESS_KEY``, ``DROPBOX_ACCESS_SECRET``, ``DROPBOX_REFRESH_TOKEN``.
"""

from __future__ import annotations

import os
from typing import Any

import dropbox

ACCESS_TOKEN_TTL_SAFETY_MARGIN_SECONDS = 300
DROPBOX_ACCESS_TOKEN_REDIS_KEY = "DROPBOX_ACCESS_TOKEN"


def access_token_cache_ttl(expires_in: Any) -> int:
    """Redis TTL for a short-lived access token, minus a 5-minute safety margin.

    Leftover callers that still cache ``DROPBOX_ACCESS_TOKEN`` should use this
    instead of the raw ``expires_in`` so Redis drops the token before Dropbox
    rejects it.
    """
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 0
    return max(1, seconds - ACCESS_TOKEN_TTL_SAFETY_MARGIN_SECONDS)


def get_dropbox_client() -> dropbox.Dropbox:
    """Return a Dropbox client the SDK can auto-refresh on 401."""
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    app_key = os.getenv("DROPBOX_ACCESS_KEY")
    app_secret = os.getenv("DROPBOX_ACCESS_SECRET")
    if not all([refresh_token, app_key, app_secret]):
        raise EnvironmentError(
            "Missing one of DROPBOX_ACCESS_KEY / DROPBOX_ACCESS_SECRET / DROPBOX_REFRESH_TOKEN"
        )
    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=app_key,
        app_secret=app_secret,
    )
