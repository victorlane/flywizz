"""Live functional tests against the real Wizz Air API.

The rest of ``tests/`` pins Flywizz's behaviour against fixtures: fast,
deterministic, and blind to the thing that actually breaks this SDK, which is
Wizz Air changing the API underneath it. These tests answer the other
question -- *is the SDK stable against the live API today* -- by asserting
invariants rather than values. Prices move every hour; "a price is never zero",
"the currency matches the departure station", "a day never appears twice" do
not.

They are opt-in, because they cost real requests against someone else's
anonymous API::

    FLYWIZZ_LIVE=1 pytest tests/functional -v

Without ``FLYWIZZ_LIVE`` the whole package is skipped, so ``pytest`` stays
offline and fast by default.

Three failure modes are deliberately *not* test failures, because none of
them says anything about this SDK:

* Kasada or CloudFront refusing this IP. A ``BotGateError`` from an un-gated
  path turns the test into a skip.
* Wizz Air being down. A 5xx that survives the transport's own retries is an
  outage, not drift, so it skips too.
* An empty window. Routes get suspended; a test that needs priced days says so
  and skips rather than failing on a seasonal gap.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import httpx
import pytest

from flywizz import AsyncWizzAir, WizzAir
from flywizz.transport import (
    AsyncCachingTransport,
    AsyncWizzairTransport,
    BotGateError,
    CachingTransport,
    WizzairTransport,
)

LIVE_ENV_VAR = "FLYWIZZ_LIVE"

#: A dense, always-priced route out of the airline's home base. Also a MAC
#: route: London days can come back as LGW rather than the requested LTN.
BUSY_ROUTE = ("BUD", "LTN")
#: A thin route with one daily rotation. Near-term days here come back as
#: ``checkPrice`` placeholders, which is the shape that regressed once already.
THIN_ROUTE = ("KTW", "EIN")
#: A pair with no direct service. ``validate_route`` says no; the connecting
#: schedule finds operating days at one stop.
INDIRECT_ROUTE = ("KTW", "LIS")

#: Far enough out that inventory is published and priced inline.
PRICED_WINDOW_START_DAYS = 30
PRICED_WINDOW_LENGTH_DAYS = 30
#: Near-term days, where Wizz Air withholds the fare and sends a placeholder.
NEAR_WINDOW_LENGTH_DAYS = 7


def pytest_collection_modifyitems(config, items):
    """Mark every test in this package ``live`` and skip unless opted in."""
    if os.environ.get(LIVE_ENV_VAR):
        return
    skip = pytest.mark.skip(reason=f"live API tests: set {LIVE_ENV_VAR}=1 to run")
    for item in items:
        if "functional" in item.nodeid:
            item.add_marker(skip)


def _upstream_outage(exc: BaseException) -> str | None:
    """Describe ``exc`` if it is Wizz Air being unavailable, else ``None``.

    The transport already retries 5xx four times with backoff, so one that
    reaches here means the endpoint is genuinely down.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, BotGateError):
            return f"edge refused this runner: {current}"
        if (
            isinstance(current, httpx.HTTPStatusError)
            and current.response.status_code >= 500
        ):
            return (
                f"{current.request.url.path} answered "
                f"{current.response.status_code} after retries"
            )
        current = current.__cause__ or current.__context__
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Turn "Wizz Air is unavailable" into a skip, not a failure.

    A ``BotGateError`` raised from a path Flywizz documents as gated is a real
    assertion and stays a failure; the tests that expect the gate catch it
    themselves, so anything reaching here came from an un-gated path.
    """
    outcome = yield
    if "functional" not in item.nodeid:
        return
    exc = outcome.excinfo
    if exc:
        reason = _upstream_outage(exc[1])
        if reason:
            pytest.skip(reason)


@pytest.fixture(scope="session")
def client():
    """One cached sync client for the whole session.

    ``CachingTransport`` matters here: ``asset/map`` is 650 KB and several
    tests need it. Sharing one session also keeps the request count honest --
    the suite should read like one user browsing, not a scraper.
    """
    c = WizzAir(CachingTransport(WizzairTransport(), ttl=3600))
    yield c
    c.close()


@pytest.fixture(scope="session")
def network(client):
    return client.get_network()


@pytest.fixture
async def async_client():
    c = AsyncWizzAir(AsyncCachingTransport(AsyncWizzairTransport(), ttl=3600))
    yield c
    await c.aclose()


def priced_window() -> tuple[datetime, datetime]:
    """A window far enough ahead that fares are quoted inline."""
    start = datetime.now() + timedelta(days=PRICED_WINDOW_START_DAYS)
    return start, start + timedelta(days=PRICED_WINDOW_LENGTH_DAYS)


def near_window() -> tuple[datetime, datetime]:
    """The next week, where fares are usually withheld behind ``checkPrice``."""
    start = datetime.now()
    return start, start + timedelta(days=NEAR_WINDOW_LENGTH_DAYS)


def require(entries, what: str):
    """Skip -- rather than fail -- when the live window is simply empty.

    A suspended route or a seasonal gap is not an SDK regression.
    """
    if not entries:
        pytest.skip(f"live API returned no {what} for this window")
    return entries
