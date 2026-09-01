"""
HTTP transport seam for the Wizz Air API.

The transport owns: the httpx client, the retry policy, the User-Agent rotation,
the two-step session handshake against wizzair.com, the rotating
``X-RequestVerificationToken`` header, and API-version discovery. The rest of the
SDK (``WizzAir`` / ``AsyncWizzAir`` and ``flywizz.wire``) talks to the transport in
terms of "give me parsed JSON for this path" and nothing else.

Two adapters ship: ``WizzairTransport`` (sync) and ``AsyncWizzairTransport``
(async). ``CachingTransport`` wraps either with a TTL cache for endpoints that
change rarely (notably ``asset/map``).
"""

from __future__ import annotations

import contextlib
import logging
import random
import re
import time
from typing import Any, Dict, Optional, Protocol, Tuple

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger("Flywizz")


class WizzairException(Exception):
    """Raised when the transport cannot satisfy a request."""

    def __init__(self, message: str):
        super().__init__(f"Wizz Air API: {message}")


class BotGateError(WizzairException):
    """Raised when Kasada refuses the request (HTTP 429, empty body).

    Retrying does not help. ``search/search``, ``booking/seatmap``,
    ``booking/ancillaries`` and ``booking/passengers`` sit behind the gate; the
    rest of the API does not. Pass ``kasada_headers`` to the transport if you
    already have a live browser session to borrow tokens from.
    """

    def __init__(self, path: str):
        super().__init__(
            f"{path} is behind the Kasada bot gate (429). "
            f"Use an un-gated endpoint, or supply kasada_headers=..."
        )


class ValidationError(WizzairException):
    """The endpoint answered 400 with a list of business validation codes."""

    def __init__(self, path: str, codes: list[str]):
        self.codes = codes
        super().__init__(f"{path} rejected the request: {', '.join(codes)}")


_USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0",
)

HOMEPAGE_URL = "https://www.wizzair.com/en-gb"
API_HOST = "https://be.wizzair.com"
DEFAULT_API_VERSION = "29.14.0"
TOKEN_COOKIE = "RequestVerificationToken"
TOKEN_HEADER = "X-RequestVerificationToken"

# The SPA embeds its API base as apiUrl:"https://be.wizzair.com/29.14.0/Api".
_VERSION_RE = re.compile(r"be\.wizzair\.com/(\d+\.\d+\.\d+)/Api")


def _default_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-GB,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://www.wizzair.com",
        "Referer": HOMEPAGE_URL,
        "User-Agent": random.choice(_USER_AGENTS),
    }


def _is_transient(exc: BaseException) -> bool:
    """Retry only on network issues or 5xx. Never on 429: that is the bot gate."""
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _scrape_version(html: str) -> Optional[str]:
    match = _VERSION_RE.search(html)
    return match.group(1) if match else None


def _interpret(path: str, response: httpx.Response) -> Dict[str, Any]:
    """Turn a raw response into parsed JSON, or the right exception."""
    if response.status_code == 429:
        raise BotGateError(path)
    try:
        data = response.json()
    except ValueError as exc:
        raise WizzairException(f"non-JSON response from {path}: {exc}") from exc
    if response.status_code == 400:
        codes = data.get("validationCodes")
        if codes:
            raise ValidationError(path, codes)
        if data.get("handlerError") == "InvalidProtocol":
            raise WizzairException(
                f"{path} answered InvalidProtocol: the verification token is stale "
                f"or the request body failed model binding"
            )
    if response.status_code >= 400:
        raise WizzairException(f"{path} answered {response.status_code}: {data}")
    return data


class Transport(Protocol):
    """Sync transport interface."""

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET ``path`` relative to the API base and parse the JSON."""

    def post_json(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST ``body`` to ``path`` relative to the API base and parse the JSON."""

    def close(self) -> None:
        """Release any underlying resources (HTTP client, etc.)."""


class AsyncTransport(Protocol):
    """Async transport interface."""

    async def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET ``path`` relative to the API base and parse the JSON."""

    async def post_json(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST ``body`` to ``path`` relative to the API base and parse the JSON."""

    async def aclose(self) -> None:
        """Release any underlying resources (HTTP client, etc.)."""


class WizzairTransport:
    """Live sync HTTP transport against be.wizzair.com.

    :param client: inject your own ``httpx.Client``.
    :param api_version: pin the version segment instead of scraping the SPA.
    :param kasada_headers: ``x-kpsdk-*`` headers copied from a live browser
        session. Without them the four gated endpoints answer 429.
    """

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        api_version: Optional[str] = None,
        kasada_headers: Optional[Dict[str, str]] = None,
    ):
        self.client = client or httpx.Client(
            headers=_default_headers(), follow_redirects=True, timeout=30.0
        )
        self.api_version = api_version
        self.kasada_headers = dict(kasada_headers or {})
        self._session_ready = False

    @property
    def base(self) -> str:
        return f"{API_HOST}/{self.api_version or DEFAULT_API_VERSION}/Api"

    def close(self) -> None:
        # Best-effort cleanup: a double-close or already-broken client
        # shouldn't bubble out of __exit__/finally callers.
        with contextlib.suppress(Exception):
            self.client.close()

    def __enter__(self) -> "WizzairTransport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_session(self) -> None:
        """Warm cookies, discover the API version, mint a verification token."""
        if self._session_ready:
            return
        self._session_ready = True
        try:
            home = self.client.get(HOMEPAGE_URL)
            if self.api_version is None:
                self.api_version = _scrape_version(home.text) or DEFAULT_API_VERSION
        except httpx.HTTPError:
            logger.warning("Could not warm session from %s", HOMEPAGE_URL)
            self.api_version = self.api_version or DEFAULT_API_VERSION
        try:
            self.client.get(f"{self.base}/userSession/new")
        except httpx.HTTPError:
            logger.warning("Could not mint a verification token")

    def _auth_headers(self) -> Dict[str, str]:
        # The token rotates: read it fresh from the jar on every request.
        headers = dict(self.kasada_headers)
        token = self.client.cookies.get(TOKEN_COOKIE)
        if token:
            headers[TOKEN_HEADER] = token
        return headers

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        response = self.client.request(
            method,
            f"{self.base}/{path}",
            params=params,
            json=body,
            headers=self._auth_headers(),
        )
        if 500 <= response.status_code < 600:
            response.raise_for_status()
        return response

    def _call(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._ensure_session()
        try:
            response = self._request(method, path, params, body)
        except httpx.HTTPError as exc:
            raise WizzairException(f"request to {path} failed: {exc}") from exc
        return _interpret(path, response)

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call("GET", path, params=params)

    def post_json(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call("POST", path, body=body or {})


class AsyncWizzairTransport:
    """Live async HTTP transport against be.wizzair.com."""

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        api_version: Optional[str] = None,
        kasada_headers: Optional[Dict[str, str]] = None,
    ):
        self.client = client or httpx.AsyncClient(
            headers=_default_headers(), follow_redirects=True, timeout=30.0
        )
        self.api_version = api_version
        self.kasada_headers = dict(kasada_headers or {})
        self._session_ready = False

    @property
    def base(self) -> str:
        return f"{API_HOST}/{self.api_version or DEFAULT_API_VERSION}/Api"

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.aclose()

    async def __aenter__(self) -> "AsyncWizzairTransport":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _ensure_session(self) -> None:
        if self._session_ready:
            return
        self._session_ready = True
        try:
            home = await self.client.get(HOMEPAGE_URL)
            if self.api_version is None:
                self.api_version = _scrape_version(home.text) or DEFAULT_API_VERSION
        except httpx.HTTPError:
            logger.warning("Could not warm session from %s", HOMEPAGE_URL)
            self.api_version = self.api_version or DEFAULT_API_VERSION
        try:
            await self.client.get(f"{self.base}/userSession/new")
        except httpx.HTTPError:
            logger.warning("Could not mint a verification token")

    def _auth_headers(self) -> Dict[str, str]:
        headers = dict(self.kasada_headers)
        token = self.client.cookies.get(TOKEN_COOKIE)
        if token:
            headers[TOKEN_HEADER] = token
        return headers

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        response = await self.client.request(
            method,
            f"{self.base}/{path}",
            params=params,
            json=body,
            headers=self._auth_headers(),
        )
        if 500 <= response.status_code < 600:
            response.raise_for_status()
        return response

    async def _call(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_session()
        try:
            response = await self._request(method, path, params, body)
        except httpx.HTTPError as exc:
            raise WizzairException(f"request to {path} failed: {exc}") from exc
        return _interpret(path, response)

    async def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._call("GET", path, params=params)

    async def post_json(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._call("POST", path, body=body or {})


class CachingTransport:
    """TTL-cache wrapper over any sync :class:`Transport`.

    Caches ``get_json`` results keyed by (path, frozen-params). ``post_json`` is
    never cached: every POST endpoint on this API is either price-sensitive or a
    mutation. Use this for ``asset/map`` and the other rarely-changing reads.

    :param inner: the transport to wrap.
    :param ttl: cache lifetime in seconds. Default 1h.
    :param max_entries: drop oldest entries beyond this; keeps memory bounded.
    """

    def __init__(self, inner: Transport, ttl: float = 3600.0, max_entries: int = 256):
        self.inner = inner
        self.ttl = ttl
        self.max_entries = max_entries
        self._store: Dict[
            Tuple[str, Tuple[Tuple[str, str], ...]], Tuple[float, Dict[str, Any]]
        ] = {}

    @staticmethod
    def _key(
        path: str, params: Optional[Dict[str, Any]]
    ) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
        items = tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))
        return (path, items)

    def _evict(self) -> None:
        if len(self._store) <= self.max_entries:
            return
        for key, _ in sorted(self._store.items(), key=lambda kv: kv[1][0])[
            : len(self._store) - self.max_entries
        ]:
            self._store.pop(key, None)

    def invalidate(self) -> None:
        """Drop every cached entry."""
        self._store.clear()

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        key = self._key(path, params)
        now = time.monotonic()
        cached = self._store.get(key)
        if cached is not None and (now - cached[0]) < self.ttl:
            return cached[1]
        data = self.inner.get_json(path, params)
        self._store[key] = (now, data)
        self._evict()
        return data

    def post_json(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.inner.post_json(path, body)

    def close(self) -> None:
        self.inner.close()
