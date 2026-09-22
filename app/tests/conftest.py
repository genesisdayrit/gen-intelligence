"""Shared pytest fixtures.

Readwise unit tests should not depend on a live Redis debounce/lock
window. Tests marked ``readwise_concurrency`` exercise the real helpers
against an in-memory fake and opt out of this no-op.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def _noop_readwise_concurrency(request, monkeypatch):
    if request.node.get_closest_marker("readwise_concurrency"):
        return

    @contextmanager
    def _immediate_lock(*_args, **_kwargs):
        yield True

    monkeypatch.setattr(
        "services.obsidian.add_readwise_buffet.lock_readwise_target",
        _immediate_lock,
        raising=False,
    )
    monkeypatch.setattr(
        "services.obsidian.add_readwise_buffet.claim_reader_document_singleflight",
        lambda *_args, **_kwargs: True,
        raising=False,
    )
    monkeypatch.setattr(
        "services.obsidian.add_readwise_buffet.release_reader_document_singleflight",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
