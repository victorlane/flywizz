"""Per-endpoint shape and invariant checks against the live API.

Each test names the endpoint it guards, so a failure points straight at the
parser in ``flywizz/wire.py`` and the section of
``docs/internal-api-spec.md`` that is now wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from flywizz.misc import (
    MAX_ARRIVAL_STATIONS,
    MAX_FLIGHT_DATES_WINDOW_DAYS,
    AvailabilitySearch,
    FareChartSearch,
    TimetableSearch,
)
from flywizz.transport import BotGateError, WizzairException

from .conftest import (
    BUSY_ROUTE,
    INDIRECT_ROUTE,
    THIN_ROUTE,
    near_window,
    priced_window,
    require,
)

ROUTES = [BUSY_ROUTE, THIN_ROUTE]
ROUTE_IDS = [f"{o}-{d}" for o, d in ROUTES]


# --- asset/map ------------------------------------------------------------


def test_network_is_populated_and_well_formed(network):
    assert len(network.stations) >= 100, "the route map lost most of its stations"
    for station in network.stations:
        assert len(station.iata) == 3 and station.iata.isupper()
        assert len(station.currency_code) == 3
        assert -90 <= station.latitude <= 90
        assert -180 <= station.longitude <= 180
    assert len({s.iata for s in network.stations}) == len(network.stations)


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_sample_routes_are_still_flown(client, origin, destination):
    """The routes the rest of this suite probes must actually exist."""
    assert client.validate_route(origin, destination) is True
    assert destination in {s.iata for s in client.get_destinations(origin)}


def test_validate_route_rejects_a_route_that_does_not_exist(client):
    assert client.validate_route("BUD", "ZZZ") is False
    assert client.get_destinations("ZZZ") == []


def test_explore_by_country_partitions_the_destinations(client):
    grouped = client.explore_by_country(BUSY_ROUTE[0])
    destinations = client.get_destinations(BUSY_ROUTE[0])
    assert sum(len(v) for v in grouped.values()) == len(destinations)
    assert all(len(code) == 2 for code in grouped)


# --- search/flightDates ---------------------------------------------------


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_flight_dates_are_ordered_datetimes_in_window(client, origin, destination):
    start, end = priced_window()
    dates = require(
        client.get_flight_dates(origin, destination, start, end), "flight dates"
    )
    assert all(isinstance(d, datetime) for d in dates)
    assert dates == sorted(dates), "flightDates came back out of order"
    assert len(set(dates)) == len(dates), "flightDates repeated a day"
    assert start.date() <= dates[0].date() and dates[-1].date() <= end.date()


# --- search/timetableV2 ---------------------------------------------------


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_timetable_days_are_unique_ordered_and_in_window(client, origin, destination):
    start, end = priced_window()
    entries = require(
        client.get_timetable(
            TimetableSearch(
                origin=origin, destination=destination, date_from=start, date_to=end
            )
        ),
        "timetable days",
    )
    days = [e.departure_date.date() for e in entries]
    assert days == sorted(days), "timetable days came back out of order"
    assert len(set(days)) == len(days), "the same day appeared twice"
    assert start.date() <= days[0] and days[-1] <= end.date()
    assert all(e.departure_station == origin for e in entries)
    # London is a MAC: asking for LTN can legitimately answer LGW, but only
    # on a day flagged as such.
    assert all(
        e.arrival_station == destination or e.has_mac_flight for e in entries
    ), "an unexpected arrival station on a day not flagged hasMacFlight"


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_timetable_departures_belong_to_their_day(client, origin, destination):
    start, end = priced_window()
    entries = require(
        client.get_timetable(
            TimetableSearch(
                origin=origin, destination=destination, date_from=start, date_to=end
            )
        ),
        "timetable days",
    )
    for entry in entries:
        assert entry.departures, f"{entry.departure_date.date()} has no departure times"
        times = [d.departure for d in entry.departures]
        assert times == sorted(times), "departure times came back out of order"
        assert all(t.date() == entry.departure_date.date() for t in times)
        cheapest = [d for d in entry.departures if d.is_cheapest_of_the_day]
        assert len(cheapest) <= 1, "more than one departure flagged cheapest of the day"


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_timetable_prices_are_real_money(client, network, origin, destination):
    """No zero fares, and always in the departure station's currency.

    The zero check is the regression guard: Wizz Air sends ``amount: 0`` as a
    placeholder on days it will not quote, and taking that literally makes an
    unpriced day look free -- and cheapest -- to every caller.
    """
    start, end = priced_window()
    entries = require(
        client.get_timetable(
            TimetableSearch(
                origin=origin, destination=destination, date_from=start, date_to=end
            )
        ),
        "timetable days",
    )
    expected_currency = network.station(origin).currency_code
    priced = [e for e in entries if e.price is not None]
    if not priced:
        pytest.skip(f"{origin}-{destination} quoted no fares in this window")

    for entry in priced:
        assert entry.price.amount > 0, (
            f"{entry.departure_date.date()} came back priced at zero "
            f"(priceType={entry.price_type!r})"
        )
        assert entry.price.currency == expected_currency, (
            f"prices arrived in {entry.price.currency} but {origin} is an "
            f"{expected_currency} station"
        )
        if entry.original_price is not None:
            assert entry.original_price.amount >= entry.price.amount
            assert entry.original_price.currency == expected_currency


@pytest.mark.parametrize("origin,destination", ROUTES, ids=ROUTE_IDS)
def test_near_term_days_withhold_the_fare_rather_than_zero_it(
    client, origin, destination
):
    """The near window is where the placeholder shape actually appears.

    ``priced_window`` is far enough out that everything is quoted inline, so
    only this window exercises ``checkPrice`` / ``noData``. Days carrying one
    of those types must expose ``price is None``.
    """
    start, end = near_window()
    entries = require(
        client.get_timetable(
            TimetableSearch(
                origin=origin, destination=destination, date_from=start, date_to=end
            )
        ),
        "near-term timetable days",
    )
    placeholders = [e for e in entries if e.price_type in ("checkPrice", "noData")]
    if not placeholders:
        pytest.skip("no placeholder days in the next week on this route")
    for entry in placeholders:
        assert entry.price is None, (
            f"{entry.departure_date.date()} is {entry.price_type!r} but still "
            f"reports a fare of {entry.price.amount}"
        )


def test_return_timetable_fills_both_directions(client):
    origin, destination = BUSY_ROUTE
    start, end = priced_window()
    outbound, inbound = client.get_return_timetable(
        TimetableSearch(
            origin=origin,
            destination=destination,
            date_from=start,
            date_to=end,
            return_date_from=start,
            return_date_to=end,
        )
    )
    require(outbound, "outbound days")
    require(inbound, "inbound days")
    assert all(e.departure_station == origin for e in outbound)
    assert all(e.departure_station in (destination, "LGW") for e in inbound)


def test_return_timetable_needs_a_return_window(client):
    start, end = priced_window()
    with pytest.raises(ValueError):
        client.get_return_timetable(
            TimetableSearch(
                origin=BUSY_ROUTE[0],
                destination=BUSY_ROUTE[1],
                date_from=start,
                date_to=end,
            )
        )


# --- asset/farechart ------------------------------------------------------


def test_fare_chart_returns_a_symmetric_strip(client, network):
    origin, destination = BUSY_ROUTE
    start, _ = priced_window()
    interval = 3
    entries = require(
        client.get_fare_chart(
            FareChartSearch(
                origin=origin,
                destination=destination,
                date=start,
                day_interval=interval,
            )
        ),
        "fare chart days",
    )
    assert len(entries) == 2 * interval + 1, (
        f"dayInterval={interval} should give {2 * interval + 1} days, "
        f"got {len(entries)}"
    )
    days = [e.day.date() for e in entries]
    assert days == sorted(days)
    assert days[0] == (start - timedelta(days=interval)).date()
    assert days[-1] == (start + timedelta(days=interval)).date()

    expected_currency = network.station(origin).currency_code
    for entry in entries:
        if entry.price is None:
            continue
        assert entry.price.amount > 0, (
            f"{entry.day.date()} came back priced at zero "
            f"(priceType={entry.price_type!r})"
        )
        # A MAC day is quoted from a different station, so its currency is
        # that station's, not the one we asked for.
        if not entry.has_mac_flight:
            assert entry.price.currency == expected_currency


# --- asset/flightinformation ----------------------------------------------


def test_flight_status_answers_for_a_live_flight_number(client):
    statuses = client.get_flight_status("W6", "2463")
    if not statuses:
        pytest.skip("W6 2463 is not operating in the window the API reports on")
    for status in statuses:
        assert status.carrier_code == "W6"
        assert len(status.departure_airport) == 3
        assert len(status.arrival_airport) == 3


# --- reference data -------------------------------------------------------


def test_currencies_are_iso_4217_codes(client):
    currencies = require(client.get_currencies(), "currencies")
    assert all(len(c) == 3 and c.isupper() for c in currencies)
    assert "EUR" in currencies


def test_countries_carry_codes_and_membership_flags(client):
    countries = require(client.get_countries(), "countries")
    codes = {c.code for c in countries}
    assert {"HU", "PL", "NL"} <= codes
    assert all(len(c.code) == 2 for c in countries)


def test_cultures_are_language_tags(client):
    cultures = require(client.get_cultures(), "cultures")
    assert any(c.code.lower().startswith("en") for c in cultures)


def test_service_fees_and_wdc_prices_are_populated(client):
    assert client.get_service_fees(["EUR"]), "asset/serviceFees came back empty"
    assert client.get_wdc_prices(), "asset/wdcPrice came back empty"


# --- the bot gate ---------------------------------------------------------


def test_search_search_is_still_gated(client):
    """The gate is documented behaviour; if it lifts, the docs are stale.

    A lifted gate is good news -- fare bundles and sell keys become reachable
    -- but it is still drift, and ``docs/internal-api-spec.md`` would need
    updating.
    """
    start, _ = priced_window()
    with pytest.raises((BotGateError, WizzairException)) as caught:
        client.get_availability(
            AvailabilitySearch(
                origin=BUSY_ROUTE[0], destination=BUSY_ROUTE[1], departure_date=start
            )
        )
    assert isinstance(caught.value, BotGateError), (
        f"search/search answered {caught.value} instead of the Kasada gate"
    )


# --- search/FlightDatesMultiArrival ---------------------------------------


def test_multi_arrival_answers_for_every_destination_asked_for(client):
    """Including the ones upstream omits, which must come back empty."""
    origin = BUSY_ROUTE[0]
    start, end = priced_window()
    destinations = [s.iata for s in client.get_destinations(origin)][:12]
    require(destinations, "destinations")

    dates = client.get_flight_dates_multi(origin, destinations, start, end)
    assert list(dates) == destinations, "a destination went missing in batching"
    for iata, days in dates.items():
        assert all(start.date() <= d.date() <= end.date() for d in days), (
            f"{iata} returned a date outside the requested window"
        )
        assert days == sorted(days)
        assert len(set(days)) == len(days)
    assert any(days for days in dates.values()), "no route operated at all"


def test_multi_arrival_agrees_with_the_single_route_endpoint(client):
    """Both schedule surfaces describe the same route, so they must match."""
    origin, destination = THIN_ROUTE
    start, end = priced_window()

    single = client.get_flight_dates(origin, destination, start, end)
    batched = client.get_flight_dates_multi(origin, [destination], start, end)
    assert batched[destination] == single


def test_multi_arrival_batches_past_the_arrival_limit(client):
    """More than five destinations must still come back fully populated.

    Upstream answers an over-long list with an empty body rather than an
    error, so an un-batched call would look exactly like "nothing operates".
    """
    origin = BUSY_ROUTE[0]
    start, end = priced_window()
    destinations = [s.iata for s in client.get_destinations(origin)][
        : MAX_ARRIVAL_STATIONS * 3
    ]
    if len(destinations) <= MAX_ARRIVAL_STATIONS:
        pytest.skip(f"{origin} has too few destinations to exercise batching")

    dates = client.get_flight_dates_multi(origin, destinations, start, end)
    assert len(dates) == len(destinations)
    assert any(days for days in dates.values())


def test_multi_arrival_rejects_an_over_long_window(client):
    start, _ = priced_window()
    with pytest.raises(ValueError, match="at most"):
        client.get_flight_dates_multi(
            BUSY_ROUTE[0],
            [BUSY_ROUTE[1]],
            start,
            start + timedelta(days=MAX_FLIGHT_DATES_WINDOW_DAYS + 1),
        )


# --- search/dohopFlightDates ----------------------------------------------


def test_connecting_dates_reach_a_route_with_no_direct_service(client):
    """The point of this endpoint: pairs the route map calls unreachable.

    `validate_route` covers direct service only, so a `False` there does not
    mean you cannot fly it.
    """
    origin, destination = INDIRECT_ROUTE
    start, end = priced_window()
    if client.validate_route(origin, destination):
        pytest.skip(f"{origin}-{destination} now flies direct; pick another pair")

    dates = require(
        client.get_connecting_flight_dates(origin, destination, start, end),
        "connecting dates",
    )
    assert all(not d.is_direct for d in dates), (
        "a route with no direct service returned direct days"
    )
    assert all(d.stops >= 1 for d in dates)


def test_connecting_dates_are_ordered_and_in_window(client):
    origin, destination = BUSY_ROUTE
    start, end = priced_window()
    dates = require(
        client.get_connecting_flight_dates(origin, destination, start, end),
        "connecting dates",
    )
    days = [d.date for d in dates]
    assert days == sorted(days)
    assert start.date() <= days[0].date() and days[-1].date() <= end.date()
    assert all(d.stops >= 0 for d in dates)
    assert all(d.is_direct == (d.stops == 0) for d in dates)
    # A day may appear twice -- direct and connecting -- but never as two
    # identical rows, which is what upstream sends for a twice-daily route.
    pairs = [(d.date, d.stops) for d in dates]
    assert len(set(pairs)) == len(pairs), "an identical row survived deduplication"


def test_connecting_dates_agree_with_flight_dates_on_direct_days(client):
    """Both surfaces describe the same direct schedule, give or take a day.

    They are computed from different snapshots, so a day or two of drift is
    normal; a large divergence means one of them changed meaning.
    """
    origin, destination = THIN_ROUTE
    start, end = priced_window()
    direct = {
        d.date.date()
        for d in client.get_connecting_flight_dates(origin, destination, start, end)
        if d.is_direct
    }
    plain = {d.date() for d in client.get_flight_dates(origin, destination, start, end)}
    require(direct, "direct connecting dates")
    require(plain, "flight dates")

    overlap = len(direct & plain) / len(direct | plain)
    assert overlap >= 0.8, (
        f"dohopFlightDates and flightDates agree on only {overlap:.0%} of direct "
        f"days (dohop-only={sorted(direct - plain)}, plain-only={sorted(plain - direct)})"
    )


def test_connecting_dates_reject_an_over_long_window(client):
    start, _ = priced_window()
    with pytest.raises(ValueError, match="at most"):
        client.get_connecting_flight_dates(
            *BUSY_ROUTE, start, start + timedelta(days=MAX_FLIGHT_DATES_WINDOW_DAYS + 1)
        )
