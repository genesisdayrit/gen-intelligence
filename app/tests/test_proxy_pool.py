"""Proxy pool: port parsing, random pick, bench, failover, redaction."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.youtube.proxy_pool import (
    ProxyEndpoint,
    ProxyPool,
    build_proxy_url,
    classify_error,
    endpoints_from_env,
    parse_ports,
    redact_secrets,
    secrets_from_env,
)


class FirstChoice:
    """Deterministic RNG: always pick the first remaining candidate."""

    def choice(self, seq):
        return seq[0]

    def shuffle(self, seq):
        return None


def _ep(port: int, user: str = "user", password: str = "s3cret") -> ProxyEndpoint:
    return ProxyEndpoint(
        url=build_proxy_url("isp.oxylabs.io", user, password, port),
        label=str(port),
    )


def test_parse_ports_range_and_list():
    assert parse_ports("8001-8005") == [8001, 8002, 8003, 8004, 8005]
    assert parse_ports("8001,8004") == [8001, 8004]
    assert parse_ports("8001-8003,8008") == [8001, 8002, 8003, 8008]
    assert parse_ports("8005-8001") == [8001, 8002, 8003, 8004, 8005]
    assert parse_ports("") == []
    assert parse_ports(None) == []
    assert parse_ports("8001, 8001, 8002") == [8001, 8002]
    assert parse_ports("nope,8003,800x-8010") == [8003]


def test_build_proxy_url_encodes_credentials():
    url = build_proxy_url("isp.oxylabs.io", "user name", "p@ss/w:rd", 8001)
    assert url == "http://user%20name:p%40ss%2Fw%3Ard@isp.oxylabs.io:8001"
    assert "p@ss" not in url


def test_endpoints_from_env_oxylabs_and_proxy_urls():
    env = {
        "OXYLABS_ISP_HOST": "isp.oxylabs.io",
        "OXYLABS_ISP_USERNAME": "user",
        "OXYLABS_ISP_PASSWORD": "s3cret",
        "OXYLABS_ISP_PORTS": "8001-8002",
        "PROXY_URLS": "http://a:b@other.host:9001, http://a:b@other.host:9002",
    }
    endpoints = endpoints_from_env(env)
    assert [ep.label for ep in endpoints] == ["8001", "8002", "9001", "9002"]
    assert all("s3cret" in ep.url or "other.host" in ep.url for ep in endpoints)


def test_endpoints_from_env_missing_oxylabs_still_uses_proxy_urls():
    endpoints = endpoints_from_env({"PROXY_URLS": "http://host:8080"})
    assert len(endpoints) == 1
    assert endpoints[0].label == "8080"


def test_pick_skips_benched_and_tried():
    e1, e2, e3 = _ep(8001), _ep(8002), _ep(8003)
    pool = ProxyPool([e1, e2, e3])
    pool.bench(e2, now=1000.0)
    picked = pool.pick(tried={e1.url}, now=1000.0, rng=FirstChoice())
    assert picked == e3


def test_pick_when_all_benched_still_picks_untried():
    e1, e2 = _ep(8001), _ep(8002)
    pool = ProxyPool([e1, e2])
    pool.bench(e1, now=1000.0)
    pool.bench(e2, now=1000.0)
    picked = pool.pick(tried=set(), now=1000.0, rng=FirstChoice())
    assert picked == e1
    picked_next = pool.pick(tried={e1.url}, now=1000.0, rng=FirstChoice())
    assert picked_next == e2
    assert pool.pick(tried={e1.url, e2.url}, now=1000.0, rng=FirstChoice()) is None


def test_bench_expires_after_60s():
    e1 = _ep(8001)
    pool = ProxyPool([e1], bench_seconds=60)
    pool.bench(e1, now=1000.0)
    assert pool.is_benched(e1, now=1059.0)
    assert not pool.is_benched(e1, now=1060.0)


def test_iter_attempts_empty_pool_is_direct():
    pool = ProxyPool([])
    assert list(pool.iter_attempts()) == [None]


def test_iter_attempts_max_equals_pool_size_and_failsover():
    e1, e2 = _ep(8001), _ep(8002)
    pool = ProxyPool([e1, e2])
    attempts = list(pool.iter_attempts(rng=FirstChoice()))
    assert [ep.label for ep in attempts] == ["8001", "8002"]
    pool.bench(e1, now=50.0)
    attempts = list(pool.iter_attempts(now=50.0, rng=FirstChoice()))
    assert attempts[0] == e2
    assert attempts[1] == e1


def test_classify_error_classes():
    assert classify_error("ERROR: Sign in to confirm you're not a bot") == "bot_wall"
    assert classify_error("HTTP 429 Too Many Requests") == "rate_limited"
    assert classify_error("timed out") == "other"
    assert classify_error("no formats and no duration (bot wall)") == "bot_wall"


def test_redact_secrets_strips_userinfo_and_raw_password():
    url = build_proxy_url("isp.oxylabs.io", "user", "s3cret", 8001)
    secrets = secrets_from_env(
        {
            "OXYLABS_ISP_USERNAME": "user",
            "OXYLABS_ISP_PASSWORD": "s3cret",
        }
    )
    message = f"failed via {url} password=s3cret"
    redacted = redact_secrets(message, secrets)
    assert "s3cret" not in redacted
    assert "user:s3cret" not in redacted
    assert "http://***:***@isp.oxylabs.io:8001" in redacted


def test_redact_secrets_regex_without_known_secrets():
    redacted = redact_secrets("boom http://alice:hunter2@host:8001 timeout")
    assert "hunter2" not in redacted
    assert "alice" not in redacted
    assert "http://***:***@host:8001" in redacted
