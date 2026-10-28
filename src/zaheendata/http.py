"""Polite, resilient HTTP client shared by all connectors.

Features: global + per-host concurrency limits, per-host token-bucket rate limiting, retries with
exponential back-off (honouring ``Retry-After``), an on-disk response cache so paid API calls are
never repeated within the TTL, robots.txt enforcement for crawled sites, and a cost ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .logging import get_logger
from .settings import Settings, get_settings

log = get_logger(__name__)


class RetryableHTTPError(Exception):
    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"retryable HTTP status {status}")
        self.status = status
        self.retry_after = retry_after


class RobotsDisallowed(Exception):
    pass


# --------------------------------------------------------------------------- rate limiting
class TokenBucket:
    def __init__(self, rate: float, burst: int | None = None):
        self.rate = max(rate, 0.01)
        self.capacity = burst or max(1, int(self.rate * 2))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


# --------------------------------------------------------------------------- cache
class ResponseCache:
    """Tiny SQLite cache keyed by request fingerprint. Safe for a single process; swap for Redis
    (``settings.redis_url``) when running many workers."""

    def __init__(self, path: Path, ttl_seconds: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, status INTEGER, headers TEXT,"
            " body BLOB, created REAL)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    @staticmethod
    def fingerprint(method: str, url: str, params: Any, body: Any) -> str:
        payload = json.dumps([method.upper(), url, params, body], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    async def get(self, key: str) -> tuple[int, dict[str, str], bytes] | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT status, headers, body, created FROM cache WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        status, headers, body, created = row
        if time.time() - created > self.ttl:
            return None
        return status, json.loads(headers), body

    async def put(self, key: str, status: int, headers: dict[str, str], body: bytes) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                (key, status, json.dumps(headers), body, time.time()),
            )
            self._conn.commit()


# --------------------------------------------------------------------------- cost ledger
@dataclass
class CostLedger:
    """Counts calls per source and estimates spend using unit prices (USD per call)."""

    unit_prices: dict[str, float] = field(
        default_factory=lambda: {
            "google_places.text_search": 0.032,
            "google_places.details": 0.020,
            "google_places.geocode": 0.005,
            "yelp.search": 0.0,
            "sam_gov.entities": 0.0,
            "census.cbp": 0.0,
            "socrata.query": 0.0,
        }
    )
    calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    cached: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record(self, meter: str, cached: bool = False) -> None:
        if cached:
            self.cached[meter] += 1
        else:
            self.calls[meter] += 1

    def estimated_usd(self) -> float:
        return round(sum(self.unit_prices.get(m, 0.0) * n for m, n in self.calls.items()), 4)

    def summary(self) -> dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "cache_hits": dict(self.cached),
            "estimated_usd": self.estimated_usd(),
        }


# --------------------------------------------------------------------------- robots
class RobotsPolicy:
    def __init__(self, user_agent: str, client: httpx.AsyncClient):
        self.user_agent = user_agent
        self._client = client
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._lock:
            if origin not in self._parsers:
                self._parsers[origin] = await self._fetch(origin)
        parser = self._parsers[origin]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    async def _fetch(self, origin: str) -> RobotFileParser | None:
        try:
            resp = await self._client.get(origin + "/robots.txt", timeout=8.0)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or len(resp.content) > 200_000:
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


# --------------------------------------------------------------------------- client
class HttpClient:
    def __init__(
        self,
        settings: Settings | None = None,
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings or get_settings()
        s = self.settings
        self._client = httpx.AsyncClient(
            headers={"User-Agent": s.user_agent, "Accept-Language": "en-US,en;q=0.9"},
            timeout=s.http_timeout_seconds,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=s.global_concurrency * 2, max_keepalive_connections=20),
            transport=transport,
        )
        self.cache = cache or ResponseCache(s.http_cache_path, s.http_cache_ttl_seconds)
        self.ledger = CostLedger()
        self.robots = RobotsPolicy(s.user_agent, self._client)
        self._global = asyncio.Semaphore(s.global_concurrency)
        self._host_sem: dict[str, asyncio.Semaphore] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._host_rates: dict[str, float] = {}

    def set_host_rate(self, host: str, requests_per_second: float, concurrency: int | None = None) -> None:
        self._host_rates[host] = requests_per_second
        self._buckets[host] = TokenBucket(requests_per_second)
        if concurrency:
            self._host_sem[host] = asyncio.Semaphore(concurrency)

    def _bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(
                self._host_rates.get(host, self.settings.per_host_requests_per_second)
            )
        return self._buckets[host]

    def _sem(self, host: str) -> asyncio.Semaphore:
        if host not in self._host_sem:
            self._host_sem[host] = asyncio.Semaphore(self.settings.per_host_concurrency)
        return self._host_sem[host]

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str] | None = None,
        meter: str | None = None,
        use_cache: bool = True,
        check_robots: bool = False,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        host = urlsplit(url).netloc
        key = self.cache.fingerprint(method, url, params, json_body or data)
        cache_headers = {k.lower() for k in (headers or {})} & {"authorization", "x-api-key"}
        cacheable = use_cache and method.upper() in {"GET", "POST"}
        if cacheable:
            hit = await self.cache.get(key if not cache_headers else key + ":auth")
            if hit:
                status, hdrs, body = hit
                if meter:
                    self.ledger.record(meter, cached=True)
                return httpx.Response(status, headers=hdrs, content=body, request=httpx.Request(method, url))

        if check_robots and self.settings.respect_robots_txt and not await self.robots.allowed(url):
            raise RobotsDisallowed(url)

        async def _once() -> httpx.Response:
            async with self._global, self._sem(host):
                await self._bucket(host).acquire()
                if max_bytes:
                    async with self._client.stream(
                        method, url, params=params, json=json_body, data=data, headers=headers
                    ) as stream:
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in stream.aiter_bytes():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > max_bytes:
                                break
                        resp = httpx.Response(
                            stream.status_code,
                            headers=stream.headers,
                            content=b"".join(chunks),
                            request=stream.request,
                        )
                else:
                    resp = await self._client.request(
                        method, url, params=params, json=json_body, data=data, headers=headers
                    )
            if resp.status_code in (408, 425, 429, 500, 502, 503, 504):
                ra = resp.headers.get("retry-after")
                raise RetryableHTTPError(resp.status_code, float(ra) if ra and ra.isdigit() else None)
            return resp

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.settings.http_max_retries),
                wait=wait_exponential_jitter(initial=1, max=30),
                retry=retry_if_exception_type((RetryableHTTPError, httpx.TransportError)),
                reraise=False,
            ):
                with attempt:
                    try:
                        resp = await _once()
                    except RetryableHTTPError as exc:
                        if exc.retry_after:
                            await asyncio.sleep(min(exc.retry_after, 60))
                        raise
        except RetryError as exc:
            last = exc.last_attempt.exception()
            log.warning("http.giveup", url=url, error=str(last))
            raise last if last else exc

        if meter:
            self.ledger.record(meter)
        if cacheable and resp.status_code == 200:
            await self.cache.put(
                key if not cache_headers else key + ":auth",
                resp.status_code,
                {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "x-shopid")},
                resp.content,
            )
        return resp

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def get_json(self, url: str, **kw: Any) -> Any:
        resp = await self.get(url, **kw)
        resp.raise_for_status()
        return resp.json()

    async def download(self, url: str, path: Path, chunk_size: int = 1 << 20) -> Path:
        """Stream a (possibly multi-GB) file to disk, bypassing the response cache."""
        host = urlsplit(url).netloc
        tmp = path.with_suffix(path.suffix + ".part")
        async with self._global, self._sem(host):
            await self._bucket(host).acquire()
            async with self._client.stream("GET", url, timeout=None) as stream:
                stream.raise_for_status()
                with tmp.open("wb") as fh:
                    async for chunk in stream.aiter_bytes(chunk_size):
                        fh.write(chunk)
        tmp.replace(path)
        return path

    async def post_json(self, url: str, **kw: Any) -> Any:
        resp = await self.post(url, **kw)
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
