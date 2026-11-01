"""Tests for the shared Dropbox client helper."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from services.dropbox.client import (
    ACCESS_TOKEN_TTL_SAFETY_MARGIN_SECONDS,
    access_token_cache_ttl,
    get_dropbox_client,
)


@patch("services.dropbox.client.dropbox.Dropbox")
def test_get_dropbox_client_uses_refresh_token_constructor(mock_dropbox):
    get_dropbox_client()
    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token="test-refresh",
        app_key="test-key",
        app_secret="test-secret",
    )
    kwargs = mock_dropbox.call_args.kwargs
    assert "oauth2_access_token" not in kwargs
    assert mock_dropbox.call_args.args == ()


@patch("services.dropbox.client.dropbox.Dropbox")
def test_get_dropbox_client_keeps_existing_env_var_names(mock_dropbox, monkeypatch):
    monkeypatch.setenv("DROPBOX_REFRESH_TOKEN", "refresh-from-env")
    monkeypatch.setenv("DROPBOX_ACCESS_KEY", "key-from-env")
    monkeypatch.setenv("DROPBOX_ACCESS_SECRET", "secret-from-env")
    get_dropbox_client()
    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token="refresh-from-env",
        app_key="key-from-env",
        app_secret="secret-from-env",
    )


@pytest.mark.parametrize("missing", ["DROPBOX_REFRESH_TOKEN", "DROPBOX_ACCESS_KEY", "DROPBOX_ACCESS_SECRET"])
@patch("services.dropbox.client.dropbox.Dropbox")
def test_get_dropbox_client_requires_all_credentials(mock_dropbox, missing, monkeypatch):
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(EnvironmentError, match="DROPBOX_"):
        get_dropbox_client()
    mock_dropbox.assert_not_called()


def test_access_token_cache_ttl_applies_five_minute_safety_margin():
    assert ACCESS_TOKEN_TTL_SAFETY_MARGIN_SECONDS == 300
    assert access_token_cache_ttl(14400) == 14100
    assert access_token_cache_ttl(300) == 1
    assert access_token_cache_ttl(60) == 1
    assert access_token_cache_ttl(None) == 1
    assert access_token_cache_ttl("not-a-number") == 1
