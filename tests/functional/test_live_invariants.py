"""Cross-endpoint and cross-call consistency against the live API.

Individual endpoints can each look healthy while disagreeing with one another,
and that disagreement is what a caller actually feels. These tests hold the
surfaces against each other and against the derived helpers built on top of
them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from flywizz.misc import FareChartSearch, TimetableSearch

from .conftest import BUSY_ROUTE, THIN_ROUTE, priced_window, require

ROUTES = [BUSY_ROUTE, THIN_ROUTE]
ROUTE_IDS = [f"{o}-{d}" for o, d in ROUTES]


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_flight_dates_and_timetable_broadly_agree(client, origin, destination):
    """The cheap schedule endpoint and the priced one describe one schedule.

    They are not identical in practice: ``flightDates`` excludes the window's
    ``to`` day, and the two are computed from different snapshots, so a day or
    two of disagreement is normal. A large divergence means one of them changed
    meaning.
    """
    start, end = priced_window()
    dates = {d.date() for d in client.get_flight_dates(origin, destination, start, end)}
    entries = client.get_timetable(
        TimetableSearch(
            origin=origin, destination=destination, date_from=start, date_to=end
        )
    )
    days = {e.departure_date.date() for e in entries}
    require(dates, "flight dates")
    require(days, "timetable days")

    overlap = len(dates & days) / len(dates | days)
    assert overlap >= 0.8, (
        f"{origin}-{destination}: flightDates and timetableV2 agree on only "
        f"{overlap:.0%} of days (flightDates-only={sorted(dates - days)}, "
        f"timetable-only={sorted(days - dates)})"
    )


def test_fare_chart_and_timetable_quote_the_same_days(client):
    """Both priced surfaces should quote one fare per day, not two.

    Prices genuinely move between two calls, so this asserts that most days
    match rather than all of them; a systematic mismatch means the strip is
    misaligned or reading a different fare.
    """
    origin, destination = BUSY_ROUTE
    target, _ = priced_window()
    interval = 3
    strip = require(
        client.get_fare_chart(
            FareChartSearch(
                origin=origin,
                destination=destination,
                date=target,
                day_interval=interval,
            )
        ),
        "fare chart days",
    )
    timetable = {
        e.departure_date.date(): e
        for e in client.get_timetable(
            TimetableSearch(
                origin=origin,
                destination=destination,
                date_from=target - timedelta(days=interval),
                date_to=target + timedelta(days=interval),
            )
        )
    }

    compared, matched = 0, 0
    for entry in strip:
        other = timetable.get(entry.day.date())
        if entry.price is None or other is None or other.price is None:
            continue
        # On a MAC day either surface may quote a neighbouring airport. That is
        # a different flight, so compare only where both picked the same pair.
        if (entry.departure_station, entry.arrival_station) != (
            other.departure_station,
            other.arrival_station,
        ):
            continue
        compared += 1
        matched += entry.price.amount == other.price.amount
    if compared < 3:
        pytest.skip("too few days priced on both surfaces to compare")
    assert matched / compared >= 0.7, (
        f"farechart and timetableV2 agreed on only {matched}/{compared} days"
    )


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_repeated_timetable_calls_return_the_same_schedule(
    client, origin, destination
):
    """Fares move; the schedule does not. Two calls must see the same days."""
    start, end = priced_window()
    params = TimetableSearch(
        origin=origin, destination=destination, date_from=start, date_to=end
    )
    first = require(client.get_timetable(params), "timetable days")
    second = client.get_timetable(params)
    assert [e.departure_date for e in first] == [e.departure_date for e in second]
    assert [e.price_type for e in first] == [e.price_type for e in second]


def test_cheapest_in_month_picks_a_real_minimum(client):
    """The helper's answer must be the minimum, and must be a real fare."""
    origin, destination = BUSY_ROUTE
    month = datetime.now() + timedelta(days=45)
    cheapest = client.cheapest_in_month(origin, destination, month)
    if cheapest is None:
        pytest.skip(f"no priced day for {origin}-{destination} in {month:%Y-%m}")

    assert cheapest.price.amount > 0, "the cheapest day came back free"
    assert cheapest.departure_date.month == month.month

    entries = client.get_timetable(
        TimetableSearch(
            origin=origin,
            destination=destination,
            date_from=month.replace(day=1),
            date_to=month.replace(day=28),
        )
    )
    priced = [e.price.amount for e in entries if e.price is not None]
    if priced:
        assert cheapest.price.amount <= min(priced)


def test_cheapest_weekend_returns_a_friday_and_its_return(client):
    origin, destination = BUSY_ROUTE
    pair = client.cheapest_weekend(origin, destination, months_ahead=3)
    if pair is None:
        pytest.skip(f"no priced weekend for {origin}-{destination} in three months")

    outbound, inbound = pair
    assert outbound.departure_date.weekday() == 4, "outbound is not a Friday"
    assert inbound.departure_date.date() == (
        outbound.departure_date + timedelta(days=2)
    ).date()
    assert outbound.price.amount > 0 and inbound.price.amount > 0
    assert outbound.departure_date < inbound.departure_date


def test_explore_with_fares_covers_every_destination_it_was_given(client):
    """The fan-out must return one row per destination, priced or not."""
    origin = THIN_ROUTE[0]
    start, end = priced_window()
    limit = 3
    fares = client.explore_with_fares(origin, start, end, limit=limit)
    assert len(fares) == limit

    reachable = {s.iata for s in client.get_destinations(origin)}
    assert {f.station.iata for f in fares} <= reachable
    for fare in fares:
        # An unreachable-in-window destination is legitimately unpriced, but a
        # priced one must carry both the fare and the day it belongs to.
        if fare.price is None:
            assert fare.departure_date is None
            continue
        assert fare.price.amount > 0, f"{fare.station.iata} came back free"
        assert start.date() <= fare.departure_date.date() <= end.date()
