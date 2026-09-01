"""Async parity: ``AsyncWizzAir`` must see what ``WizzAir`` sees.

``flywizz/async_client.py`` is a hand-maintained mirror of ``wizzair.py`` --
"add to both, or neither". Nothing enforces that at import time, so the two
can drift into answering differently while both look fine in isolation. These
tests run the same query down both paths and compare.
"""

from __future__ import annotations

import pytest

from flywizz import AsyncCachingTransport, CachingTransport
from flywizz.misc import FareChartSearch, TimetableSearch
from flywizz.transport import AsyncWizzairTransport, WizzairTransport

from .conftest import BUSY_ROUTE, THIN_ROUTE, priced_window, require


async def test_async_network_matches_sync(client, async_client):
    sync_codes = [s.iata for s in client.get_network().stations]
    async_codes = [s.iata for s in (await async_client.get_network()).stations]
    assert sync_codes == async_codes


async def test_async_timetable_matches_sync_schedule(client, async_client):
    """Same days, same price types. Fares may have moved between the calls."""
    origin, destination = THIN_ROUTE
    start, end = priced_window()
    params = TimetableSearch(
        origin=origin, destination=destination, date_from=start, date_to=end
    )
    sync_entries = require(client.get_timetable(params), "timetable days")
    async_entries = await async_client.get_timetable(params)

    assert [e.departure_date for e in sync_entries] == [
        e.departure_date for e in async_entries
    ]
    assert [e.price_type for e in sync_entries] == [
        e.price_type for e in async_entries
    ]
    assert [e.price is None for e in sync_entries] == [
        e.price is None for e in async_entries
    ]


async def test_async_fare_chart_matches_sync_days(client, async_client):
    origin, destination = BUSY_ROUTE
    target, _ = priced_window()
    params = FareChartSearch(
        origin=origin, destination=destination, date=target, day_interval=3
    )
    sync_days = [e.day for e in require(client.get_fare_chart(params), "fare days")]
    async_days = [e.day for e in await async_client.get_fare_chart(params)]
    assert sync_days == async_days


async def test_async_prices_are_real_money(async_client):
    """The zero-fare guard has to hold on the async path too."""
    origin, destination = THIN_ROUTE
    start, end = priced_window()
    entries = require(
        await async_client.get_timetable(
            TimetableSearch(
                origin=origin, destination=destination, date_from=start, date_to=end
            )
        ),
        "timetable days",
    )
    for entry in entries:
        if entry.price is not None:
            assert entry.price.amount > 0
        if entry.price_type in ("checkPrice", "noData"):
            assert entry.price is None


async def test_async_explore_with_fares_fans_out(async_client):
    origin = THIN_ROUTE[0]
    start, end = priced_window()
    fares = await async_client.explore_with_fares(origin, start, end, limit=3)
    assert len(fares) == 3
    assert all(f.price is None or f.price.amount > 0 for f in fares)


async def test_async_cache_serves_a_second_call(async_client):
    """A cached read must return the result again, not a spent coroutine.

    ``AsyncCachingTransport`` exists because the sync wrapper stored whatever
    ``get_json`` returned; against an async transport that is the coroutine,
    so the second call raised ``RuntimeError: cannot reuse already awaited
    coroutine``.
    """
    first = await async_client.get_network()
    second = await async_client.get_network()
    assert [s.iata for s in first.stations] == [s.iata for s in second.stations]


def test_cache_wrappers_reject_the_wrong_transport():
    """The two wrappers are not interchangeable, and say so loudly."""
    with pytest.raises(TypeError, match="AsyncCachingTransport"):
        CachingTransport(AsyncWizzairTransport())
    with pytest.raises(TypeError, match="CachingTransport"):
        AsyncCachingTransport(WizzairTransport())


async def test_async_cheapest_weekend_matches_sync(client, async_client):
    """Both paths chunk a three-month range; they must land on the same pair.

    ``months_ahead=3`` is wider than one timetable window, so this also proves
    the chunking works live rather than only against fixtures.
    """
    origin, destination = BUSY_ROUTE
    sync_pair = client.cheapest_weekend(origin, destination, months_ahead=3)
    async_pair = await async_client.cheapest_weekend(origin, destination, months_ahead=3)
    if sync_pair is None or async_pair is None:
        pytest.skip(f"no priced weekend for {origin}-{destination} in three months")
    assert sync_pair[0].departure_date == async_pair[0].departure_date
    assert sync_pair[1].departure_date == async_pair[1].departure_date
