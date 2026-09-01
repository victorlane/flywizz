import httpx
import pytest

from flywizz.transport import (
    AsyncCachingTransport,
    BotGateError,
    CachingTransport,
    ValidationError,
    WizzairException,
    _interpret,
    _is_transient,
    _scrape_version,
)


def _response(status: int, json_body=None, text: str = "") -> httpx.Response:
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=httpx.Request("GET", "https://x"))
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://x"))


def test_scrape_version():
    html = 'foo apiUrl:"https://be.wizzair.com/29.14.0/Api" bar'
    assert _scrape_version(html) == "29.14.0"
    assert _scrape_version("nothing here") is None


def test_bot_gate_is_not_transient():
    exc = httpx.HTTPStatusError("", request=httpx.Request("GET", "https://x"),
                                response=_response(429, text=""))
    assert _is_transient(exc) is False


def test_server_error_is_transient():
    exc = httpx.HTTPStatusError("", request=httpx.Request("GET", "https://x"),
                                response=_response(503, text=""))
    assert _is_transient(exc) is True


def test_interpret_raises_bot_gate():
    with pytest.raises(BotGateError):
        _interpret("search/search", _response(429, text=""))


def test_interpret_raises_validation_error():
    with pytest.raises(ValidationError) as info:
        _interpret("asset/farechart", _response(400, {"validationCodes": ["A", "B"]}))
    assert info.value.codes == ["A", "B"]


def test_interpret_explains_invalid_protocol():
    with pytest.raises(WizzairException, match="verification token"):
        _interpret("asset/culture", _response(400, {"handlerError": "InvalidProtocol"}))


def test_interpret_passes_success_through():
    assert _interpret("asset/currencies", _response(200, {"currencies": ["EUR"]})) == {
        "currencies": ["EUR"]
    }


class _Recorder:
    def __init__(self):
        self.gets = 0
        self.posts = 0

    def get_json(self, path, params=None):
        self.gets += 1
        return {"path": path, "n": self.gets}

    def post_json(self, path, body=None):
        self.posts += 1
        return {"path": path, "n": self.posts}

    def close(self):
        pass


def test_caching_transport_caches_gets_not_posts():
    inner = _Recorder()
    cached = CachingTransport(inner)
    assert cached.get_json("asset/map") == cached.get_json("asset/map")
    assert inner.gets == 1
    cached.post_json("search/timetableV2", {})
    cached.post_json("search/timetableV2", {})
    assert inner.posts == 2


def test_caching_transport_invalidate():
    inner = _Recorder()
    cached = CachingTransport(inner)
    cached.get_json("asset/map")
    cached.invalidate()
    cached.get_json("asset/map")
    assert inner.gets == 2


class _AsyncRecorder:
    def __init__(self):
        self.gets = 0
        self.posts = 0

    async def get_json(self, path, params=None):
        self.gets += 1
        return {"path": path, "n": self.gets}

    async def post_json(self, path, body=None):
        self.posts += 1
        return {"path": path, "n": self.posts}

    async def aclose(self):
        pass


async def test_async_caching_transport_caches_gets_not_posts():
    """The async wrapper must cache the awaited result, not the coroutine.

    Caching the coroutine let the first call succeed and the second raise
    `RuntimeError: cannot reuse already awaited coroutine`.
    """
    inner = _AsyncRecorder()
    cached = AsyncCachingTransport(inner)
    first = await cached.get_json("asset/map")
    second = await cached.get_json("asset/map")
    assert first == second
    assert inner.gets == 1

    await cached.post_json("search/timetableV2", {})
    await cached.post_json("search/timetableV2", {})
    assert inner.posts == 2


async def test_async_caching_transport_invalidate_and_close():
    inner = _AsyncRecorder()
    cached = AsyncCachingTransport(inner)
    await cached.get_json("asset/map")
    cached.invalidate()
    await cached.get_json("asset/map")
    assert inner.gets == 2
    await cached.aclose()


def test_cache_wrappers_reject_the_other_kind_of_transport():
    """Mixing them silently corrupted the cache, so it is refused up front."""
    with pytest.raises(TypeError, match="AsyncCachingTransport"):
        CachingTransport(_AsyncRecorder())
    with pytest.raises(TypeError, match="CachingTransport"):
        AsyncCachingTransport(_Recorder())
