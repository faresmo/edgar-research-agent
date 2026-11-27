"""Shared HTTP client for sec.gov.

Every request in this package goes through EdgarClient, which does three things:

1. Adds the User-Agent header the SEC requires (from EDGAR_USER_AGENT).
2. Spaces requests out so we stay under 10 per second.
3. Caches responses on disk so repeated questions don't hit the SEC again.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

USER_AGENT_ENV = "EDGAR_USER_AGENT"
DEFAULT_CACHE_DIR = ".edgar_cache"
DEFAULT_MIN_INTERVAL = 0.125  # seconds between requests = 8 per second
DEFAULT_TTL = 3600  # seconds a cached response stays fresh
FOREVER = float("inf")  # for things that never change, such as a filed document


class EdgarError(Exception):
    """Base class for all edgar_core errors."""


class EdgarConfigError(EdgarError):
    """The client is not configured correctly (for example, no User-Agent)."""


class EdgarHTTPError(EdgarError):
    """sec.gov returned an error or could not be reached."""

    def __init__(self, url: str, status: Optional[int], message: str, retry_after: Optional[float] = None):
        super().__init__(f"{message} (status={status}, url={url})")
        self.url = url
        self.status = status
        self.retry_after = retry_after


Fetcher = Callable[[str, Dict[str, str]], bytes]

NEUTRAL_AGENT = "edgar-research-agent"  # what a host other than the SEC is told: the program's name, nobody's contact


def is_sec_host(url: str) -> bool:
    """True for sec.gov and its subdomains (www, data, efts), and for nothing else."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


def _urllib_fetch(url: str, headers: Dict[str, str]) -> bytes:
    """Default fetcher: one real HTTP GET using the standard library."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body
    except urllib.error.HTTPError as exc:
        wait = None
        try:
            wait = float(exc.headers.get("Retry-After")) if exc.headers and exc.headers.get("Retry-After") else None
        except (TypeError, ValueError):
            wait = None
        raise EdgarHTTPError(url, exc.code, f"sec.gov returned HTTP {exc.code}", retry_after=wait) from exc
    except urllib.error.URLError as exc:
        raise EdgarHTTPError(url, None, f"could not reach sec.gov: {exc.reason}") from exc


class EdgarClient:
    """Rate-limited, caching client for sec.gov JSON endpoints.

    The `fetch`, `clock` and `sleep` arguments exist so tests can run offline
    and instantly. Normal code never needs to pass them.
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        cache_dir: Optional[str] = DEFAULT_CACHE_DIR,
        ttl: float = DEFAULT_TTL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        fetch: Fetcher = _urllib_fetch,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        user_agent = (user_agent or os.environ.get(USER_AGENT_ENV, "")).strip()
        if not user_agent:
            raise EdgarConfigError(
                f"Set the {USER_AGENT_ENV} environment variable to a name and contact "
                'email, for example: export EDGAR_USER_AGENT="Jane Doe jane@example.com". '
                "The SEC requires this on every request."
            )
        if min_interval < 0.1:
            raise EdgarConfigError(
                "min_interval must be at least 0.1 seconds (the SEC limit is 10 requests per second)."
            )
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl = ttl
        self.min_interval = min_interval
        self._fetch = fetch
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request: Optional[float] = None

    # -- public -----------------------------------------------------------

    def get_json(self, url: str) -> Any:
        """Return the parsed JSON at `url`, from cache if it is still fresh."""
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        body = self._request(url)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise EdgarHTTPError(url, None, "sec.gov did not return valid JSON") from exc
        self._write_cache(url, data)
        return data

    def get_text(self, url: str, ttl: Optional[float] = None, headers: Optional[Dict[str, str]] = None) -> str:
        """Return the document at `url` as text, from cache if it is still fresh.

        `ttl` overrides the client's default freshness window for this call.
        Pass FOREVER for filed documents, which never change.
        `headers` are sent with the request and are NOT kept: a key belongs there, never in `url`, because the URL
        is written to the cache with the response (until 22 Nov 2026 the price key sat in the URL, and in the cache).
        """
        cached = self._read_cache(url, ttl)
        if isinstance(cached, str):
            return cached
        text = self._request(url, accept="text/html, text/plain, */*", extra=headers).decode("utf-8", errors="replace")
        self._write_cache(url, text)
        return text

    # -- internals --------------------------------------------------------

    def _request(self, url: str, accept: str = "application/json", extra: Optional[Dict[str, str]] = None) -> bytes:
        headers = {
            # The contact (a name and an email) is what the SEC asks for, and it goes to the SEC only. Until 22 Nov 2026
            # every request carried it: the price provider and any company website the tools read received it too.
            "User-Agent": self.user_agent if is_sec_host(url) else NEUTRAL_AGENT,
            "Accept-Encoding": "gzip",
            "Accept": accept,
        }
        headers.update(extra or {})
        # 7 Nov (idea from Kestra's EDGAR plugin): SEC answers 429/503 when it wants us to slow down. Retry after its
        # Retry-After delay, then twice more, 10 and 20 seconds later, instead of failing the question.
        waits = [None, 10.0, 20.0]
        for attempt in range(len(waits) + 1):
            with self._lock:
                self._wait_for_slot()
                try:
                    return self._fetch(url, headers)
                except EdgarHTTPError as exc:
                    if exc.status not in (429, 503) or attempt == len(waits):
                        raise
                    delay = waits[attempt] if waits[attempt] is not None else min(exc.retry_after or 5.0, 60.0)
                finally:
                    self._last_request = self._clock()
            self._sleep(delay)
        raise EdgarHTTPError(url, None, "unreachable")

    def _wait_for_slot(self) -> None:
        if self._last_request is None:
            return
        elapsed = self._clock() - self._last_request
        if elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)

    def _cache_path(self, url: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_cache(self, url: str, ttl: Optional[float] = None) -> Any:
        ttl = self.ttl if ttl is None else ttl
        path = self._cache_path(url)
        if path is None or not path.exists():
            return None
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if time.time() - entry.get("fetched_at", 0) > ttl:
            return None
        return entry.get("data")

    def _write_cache(self, url: str, data: Any) -> None:
        path = self._cache_path(url)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "fetched_at": time.time(), "data": data}))
