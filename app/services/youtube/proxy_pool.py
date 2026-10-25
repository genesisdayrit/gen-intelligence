"""Process-level proxy pool for Oxylabs dedicated-ISP ports (and extras).

Modeled on Searchlore's TypeScript ProxyPool: every configured endpoint is
eligible, each try picks uniformly at random among endpoints that are not
benched and not yet tried this run, and a failure benches that endpoint for
60 seconds then fails over. Bench state is shared across calls in-process.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BENCH_SECONDS = 60
_CREDENTIAL_IN_URL = re.compile(r"https?://[^@\s/]+:[^@\s/]+@")

_default_pool: ProxyPool | None = None
_default_pool_lock = threading.Lock()


@dataclass(frozen=True)
class ProxyEndpoint:
    """One proxy URL plus a credential-free label (usually the port)."""

    url: str
    label: str


def parse_ports(spec: str | None) -> list[int]:
    """Parse ``8001-8005`` and/or ``8001,8004`` into a de-duplicated port list."""
    if not spec or not str(spec).strip():
        return []
    out: list[int] = []
    seen: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s), int(end_s)
                if start > end:
                    start, end = end, start
                ports = range(start, end + 1)
            else:
                ports = (int(part),)
        except ValueError:
            continue
        for port in ports:
            if port not in seen:
                seen.add(port)
                out.append(port)
    return out


def build_proxy_url(host: str, username: str, password: str, port: int) -> str:
    """``http://user:pass@host:port`` with URL-encoded credentials."""
    user_q = urllib.parse.quote(username, safe="")
    pass_q = urllib.parse.quote(password, safe="")
    return f"http://{user_q}:{pass_q}@{host}:{int(port)}"


def label_from_proxy_url(url: str) -> str:
    """Port (or host) for logs — never credentials."""
    parsed = urllib.parse.urlparse(url)
    if parsed.port:
        return str(parsed.port)
    return parsed.hostname or "proxy"


def secrets_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    """Username/password plus URL-encoded forms, for redaction."""
    env = os.environ if env is None else env
    secrets: list[str] = []
    for key in ("OXYLABS_ISP_USERNAME", "OXYLABS_ISP_PASSWORD"):
        value = (env.get(key) or "").strip()
        if not value:
            continue
        secrets.append(value)
        encoded = urllib.parse.quote(value, safe="")
        if encoded != value:
            secrets.append(encoded)
    return secrets


def redact_secrets(text: object, secrets: list[str] | None = None) -> str:
    """Strip proxy credentials from error text. Never leave user:pass in URLs."""
    redacted = str(text)
    for secret in secrets or ():
        if secret:
            redacted = redacted.replace(secret, "***")
    return _CREDENTIAL_IN_URL.sub("http://***:***@", redacted)


def classify_error(err: object) -> str:
    """Map a failure to ``bot_wall``, ``rate_limited``, or ``other``."""
    text = str(err).lower()
    if (
        "sign in to confirm" in text
        or "not a bot" in text
        or "bot wall" in text
        or "bot_wall" in text
    ):
        return "bot_wall"
    if "429" in text or "too many requests" in text:
        return "rate_limited"
    return "other"


def endpoints_from_env(env: Mapping[str, str] | None = None) -> list[ProxyEndpoint]:
    """Build Oxylabs port endpoints plus optional ``PROXY_URLS``."""
    env = os.environ if env is None else env
    endpoints: list[ProxyEndpoint] = []
    seen: set[str] = set()

    host = (env.get("OXYLABS_ISP_HOST") or "").strip()
    username = (env.get("OXYLABS_ISP_USERNAME") or "").strip()
    password = (env.get("OXYLABS_ISP_PASSWORD") or "").strip()
    ports = parse_ports(env.get("OXYLABS_ISP_PORTS"))
    if host and username and password and ports:
        for port in ports:
            url = build_proxy_url(host, username, password, port)
            if url not in seen:
                seen.add(url)
                endpoints.append(ProxyEndpoint(url=url, label=str(port)))

    extra = (env.get("PROXY_URLS") or "").strip()
    if extra:
        for raw in extra.split(","):
            raw = raw.strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)
            endpoints.append(ProxyEndpoint(url=raw, label=label_from_proxy_url(raw)))
    return endpoints


class ProxyPool:
    """Rotating proxy list with process-level 60s benches."""

    def __init__(
        self,
        endpoints: list[ProxyEndpoint] | None = None,
        *,
        secrets: list[str] | None = None,
        bench_seconds: float = BENCH_SECONDS,
    ) -> None:
        self.endpoints = list(endpoints or [])
        self.secrets = list(secrets or [])
        self.bench_seconds = bench_seconds
        self._bench_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProxyPool:
        env = os.environ if env is None else env
        return cls(endpoints_from_env(env), secrets=secrets_from_env(env))

    def redact(self, text: object) -> str:
        return redact_secrets(text, self.secrets)

    def is_benched(self, endpoint: ProxyEndpoint, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            return self._bench_until.get(endpoint.url, 0.0) > now

    def bench(self, endpoint: ProxyEndpoint, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._bench_until[endpoint.url] = now + self.bench_seconds

    def pick(
        self,
        tried: set[str],
        *,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> ProxyEndpoint | None:
        """Uniform random among not-benched, not-tried; if all benched, still pick."""
        now = time.monotonic() if now is None else now
        chooser = rng if rng is not None else random
        with self._lock:
            remaining = [ep for ep in self.endpoints if ep.url not in tried]
            if not remaining:
                return None
            available = [
                ep for ep in remaining if self._bench_until.get(ep.url, 0.0) <= now
            ]
            candidates = available or remaining
            return chooser.choice(candidates)

    def iter_attempts(
        self,
        *,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> Iterator[ProxyEndpoint | None]:
        """Yield the next endpoint to try. Empty pool yields a single ``None`` (direct)."""
        if not self.endpoints:
            yield None
            return
        tried: set[str] = set()
        for _ in range(len(self.endpoints)):
            endpoint = self.pick(tried, now=now, rng=rng)
            if endpoint is None:
                return
            tried.add(endpoint.url)
            yield endpoint

    def endpoint_for_url(self, url: str | None) -> ProxyEndpoint | None:
        if not url:
            return None
        for endpoint in self.endpoints:
            if endpoint.url == url:
                return endpoint
        return None


def default_pool() -> ProxyPool:
    """Process-wide pool so benches persist across YouTube fetches."""
    global _default_pool
    with _default_pool_lock:
        if _default_pool is None:
            _default_pool = ProxyPool.from_env()
        return _default_pool


def reset_default_pool() -> None:
    """Drop the process-wide pool (tests)."""
    global _default_pool
    with _default_pool_lock:
        _default_pool = None
