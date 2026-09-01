from datetime import datetime, timedelta

import pytest

from flywizz import WizzAir
from flywizz.misc import MAX_TIMETABLE_WINDOW_DAYS, FareChartSearch, TimetableSearch

NETWORK = {
    "cities": [
        {"iata": "BUD", "mac": None, "shortName": "Budapest", "countryName": "Hungary",
         "countryCode": "HU", "currencyCode": "HUF", "latitude": 47.4, "longitude": 19.2,
         "aliases": [], "categories": [], "rank": 1, "isFakeStation": False,
         "connections": [
             {"iata": "LTN", "isDirectFlight": True, "isDomestic": False,
              "isConnected": False, "isNew": False, "operationStartDate": None},
         ]},
        {"iata": "LTN", "mac": "LON", "shortName": "London Luton",
         "countryName": "United Kingdom", "countryCode": "GB", "currencyCode": "GBP",
         "latitude": 51.8, "longitude": -0.3, "aliases": [], "categories": [],
         "rank": 1, "isFakeStation": False, "connections": []},
    ]
}

TIMETABLE = {
    "outboundFlights": [
        {"departureStation": "BUD", "arrivalStation": "LTN",
         "departureDate": "2026-11-06T06:00:00",
         "price": {"amount": 20000.0, "currencyCode": "HUF"},
         "priceType": "price", "departureDates": [], "hasMacFlight": False},
        {"departureStation": "BUD", "arrivalStation": "LTN",
         "departureDate": "2026-11-13T06:00:00",
         "price": {"amount": 9000.0, "currencyCode": "HUF"},
         "priceType": "price", "departureDates": [], "hasMacFlight": False},
        {"departureStation": "BUD", "arrivalStation": "LTN",
         "departureDate": "2026-11-20T06:00:00",
         "price": None, "priceType": "soldOut", "departureDates": [],
         "hasMacFlight": False},
    ],
    "returnFlights": [],
}


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append(("GET", path, params))
        return self.responses[path]

    def post_json(self, path, body=None):
        self.calls.append(("POST", path, body))
        return self.responses[path]

    def close(self):
        pass


def test_get_destinations():
    client = WizzAir(FakeTransport({"asset/map": NETWORK}))
    assert [s.iata for s in client.get_destinations("bud")] == ["LTN"]
    assert client.get_destinations("ZZZ") == []


def test_validate_route():
    client = WizzAir(FakeTransport({"asset/map": NETWORK}))
    assert client.validate_route("BUD", "ltn") is True
    assert client.validate_route("LTN", "BUD") is False


def test_explore_by_country():
    client = WizzAir(FakeTransport({"asset/map": NETWORK}))
    assert list(client.explore_by_country("BUD")) == ["GB"]


def test_get_timetable_parses_all_days():
    client = WizzAir(FakeTransport({"search/timetableV2": TIMETABLE}))
    entries = client.get_timetable(
        TimetableSearch(origin="BUD", destination="LTN",
                        date_from=datetime(2026, 11, 1), date_to=datetime(2026, 11, 30))
    )
    assert len(entries) == 3
    assert entries[2].price is None


def test_cheapest_in_month_skips_unpriced():
    client = WizzAir(FakeTransport({"search/timetableV2": TIMETABLE}))
    cheapest = client.cheapest_in_month("BUD", "LTN", datetime(2026, 11, 15))
    assert cheapest is not None
    assert cheapest.price.amount == 9000.0


def test_cheapest_in_month_requests_full_month():
    transport = FakeTransport({"search/timetableV2": TIMETABLE})
    WizzAir(transport).cheapest_in_month("BUD", "LTN", datetime(2026, 2, 17))
    body = transport.calls[0][2]
    assert body["flightList"][0]["from"] == "2026-02-01"
    assert body["flightList"][0]["to"] == "2026-02-28"


def test_return_timetable_requires_return_window():
    client = WizzAir(FakeTransport({"search/timetableV2": TIMETABLE}))
    with pytest.raises(ValueError):
        client.get_return_timetable(
            TimetableSearch(origin="BUD", destination="LTN",
                            date_from=datetime(2026, 11, 1), date_to=datetime(2026, 11, 30))
        )


def test_fare_chart():
    client = WizzAir(FakeTransport({"asset/farechart": {
        "outboundFlights": [
            {"departureStation": "BUD", "arrivalStation": "LTN",
             "date": "2026-11-07T00:00:00",
             "price": {"amount": 9802.0, "currencyCode": "HUF"},
             "priceType": "price", "classOfService": "RS", "hasMacFlight": False}
        ]
    }}))
    entries = client.get_fare_chart(
        FareChartSearch(origin="BUD", destination="LTN", date=datetime(2026, 11, 10))
    )
    assert entries[0].price.currency == "HUF"


def test_cheapest_weekend_splits_the_window_the_api_will_not_take():
    """Three months is wider than one timetable call allows.

    The default `months_ahead=3` asks for ~93 days; the endpoint caps a window
    at 42 and rejects the rest with `InvalidTimeDateRange`. The helper has to
    walk the range in chunks, so the default keeps working.
    """
    transport = FakeTransport({"search/timetableV2": TIMETABLE})
    WizzAir(transport).cheapest_weekend("BUD", "LTN", months_ahead=3)

    windows = [c[2]["flightList"][0] for c in transport.calls]
    assert len(windows) > 1, "the three-month range was sent as one call"
    for leg in windows:
        span = (
            datetime.fromisoformat(leg["to"]) - datetime.fromisoformat(leg["from"])
        ).days
        assert span <= MAX_TIMETABLE_WINDOW_DAYS

    # The chunks must tile the range: no gap, no overlap.
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert datetime.fromisoformat(later["from"]) == datetime.fromisoformat(
            earlier["to"]
        ) + timedelta(days=1)


def test_cheapest_weekend_keeps_the_return_leg_inside_its_chunk():
    """A Friday at the edge of a chunk still needs its Sunday in the same call."""
    transport = FakeTransport({"search/timetableV2": TIMETABLE})
    WizzAir(transport).cheapest_weekend("BUD", "LTN", months_ahead=2, weekend_length=3)

    for call in transport.calls:
        outbound, inbound = call[2]["flightList"]
        assert datetime.fromisoformat(inbound["to"]) == datetime.fromisoformat(
            outbound["to"]
        ) + timedelta(days=3)
        span = (
            datetime.fromisoformat(inbound["to"])
            - datetime.fromisoformat(inbound["from"])
        ).days
        assert span <= MAX_TIMETABLE_WINDOW_DAYS
