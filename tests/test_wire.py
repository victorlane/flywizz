from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError

from flywizz.misc import (
    MAX_TIMETABLE_WINDOW_DAYS,
    AvailabilitySearch,
    FareChartSearch,
    TimetableSearch,
)
from flywizz.wire import (
    parse_fare_chart_entry,
    parse_flight_dates,
    parse_flight_status,
    parse_live_price,
    parse_network,
    parse_price,
    parse_timetable_entry,
    serialize_availability,
    serialize_fare_chart,
    serialize_flight_dates,
    serialize_timetable,
)


def test_timetable_one_way_has_single_leg():
    body = serialize_timetable(
        TimetableSearch(
            origin="bud",
            destination="ltn",
            date_from=datetime(2026, 11, 1),
            date_to=datetime(2026, 11, 30),
        )
    )
    assert body["flightList"] == [
        {
            "departureStation": "BUD",
            "arrivalStation": "LTN",
            "from": "2026-11-01",
            "to": "2026-11-30",
        }
    ]
    assert body["priceType"] == "regular"
    assert body["adultCount"] == 1


def test_timetable_return_adds_reversed_leg():
    body = serialize_timetable(
        TimetableSearch(
            origin="BUD",
            destination="LTN",
            date_from=datetime(2026, 11, 1),
            date_to=datetime(2026, 11, 10),
            return_date_from=datetime(2026, 11, 5),
            return_date_to=datetime(2026, 11, 15),
        )
    )
    assert len(body["flightList"]) == 2
    assert body["flightList"][1]["departureStation"] == "LTN"
    assert body["flightList"][1]["arrivalStation"] == "BUD"


def test_fare_chart_rejects_short_interval():
    with pytest.raises(ValueError):
        FareChartSearch(
            origin="BUD", destination="LTN", date=datetime(2026, 11, 10), day_interval=2
        )


def test_fare_chart_body():
    body = serialize_fare_chart(
        FareChartSearch(origin="BUD", destination="LTN", date=datetime(2026, 11, 10))
    )
    assert body["dayInterval"] == 3
    assert body["flightList"][0]["date"] == "2026-11-10"


def test_availability_uses_midnight_datetimes():
    body = serialize_availability(
        AvailabilitySearch(
            origin="BUD", destination="LTN", departure_date=datetime(2026, 11, 14, 9, 30)
        )
    )
    assert body["flightList"][0]["departureDate"] == "2026-11-14T00:00:00"
    assert body["wdc"] is True
    assert body["isFlightChange"] is False


def test_flight_dates_params_uppercase():
    params = serialize_flight_dates(
        "bud", "ltn", datetime(2026, 11, 1), datetime(2026, 11, 30)
    )
    assert params == {
        "departureStation": "BUD",
        "arrivalStation": "LTN",
        "from": "2026-11-01",
        "to": "2026-11-30",
    }


def test_iata_validation():
    with pytest.raises(ValueError):
        TimetableSearch(
            origin="BUDA",
            destination="LTN",
            date_from=datetime(2026, 11, 1),
            date_to=datetime(2026, 11, 2),
        )


def test_parse_price_none_amount():
    assert parse_price(None) is None
    assert parse_price({"amount": None, "currencyCode": "HUF"}) is None


def test_parse_timetable_entry():
    entry = parse_timetable_entry(
        {
            "departureStation": "BUD",
            "arrivalStation": "LTN",
            "departureDate": "2026-11-01T06:00:00",
            "price": {"amount": 23016.0, "currencyCode": "HUF",
                      "exchangedAmount": None, "exchangedCurrencyCode": None},
            "originalPrice": {"amount": 25190.0, "currencyCode": "HUF"},
            "priceType": "price",
            "departureDates": [
                {"date": "2026-11-01T06:00:00", "isCheapestOfTheDay": True},
                {"date": "2026-11-01T17:30:00", "isCheapestOfTheDay": False},
            ],
            "hasMacFlight": True,
            "appliedCouponCode": None,
        }
    )
    assert entry.price.amount == 23016.0
    assert entry.price.currency == "HUF"
    assert len(entry.departures) == 2
    assert entry.departures[0].is_cheapest_of_the_day is True


def test_parse_fare_chart_entry():
    entry = parse_fare_chart_entry(
        {
            "departureStation": "BUD",
            "arrivalStation": "LTN",
            "date": "2026-11-07T00:00:00",
            "price": {"amount": 9802.0, "currencyCode": "HUF"},
            "priceType": "price",
            "classOfService": "RS",
            "hasMacFlight": False,
        }
    )
    assert entry.day == datetime(2026, 11, 7)
    assert entry.class_of_service == "RS"


def test_parse_live_price_drops_placeholder_days():
    placeholder = {"amount": 0.0, "currencyCode": "PLN"}
    assert parse_live_price(placeholder, "checkPrice") is None
    assert parse_live_price(placeholder, "noData") is None
    # A genuine fare survives, and so does a zero on a real price type.
    assert parse_live_price({"amount": 519.0, "currencyCode": "PLN"}, "checkPrice").amount == 519.0
    assert parse_live_price(placeholder, "price").amount == 0.0


def test_parse_timetable_entry_check_price_has_no_fare():
    entry = parse_timetable_entry(
        {
            "departureStation": "KTW",
            "arrivalStation": "EIN",
            "departureDate": "2026-09-02T10:45:00",
            "price": {"amount": 0.0, "currencyCode": "PLN"},
            "originalPrice": {"amount": 644.0, "currencyCode": "PLN"},
            "priceType": "checkPrice",
            "departureDates": [{"date": "2026-09-02T10:45:00", "isCheapestOfTheDay": True}],
            "hasMacFlight": False,
            "appliedCouponCode": None,
        }
    )
    assert entry.price is None
    assert entry.original_price.amount == 644.0
    assert entry.price_type == "checkPrice"


def test_parse_fare_chart_entry_no_data_has_no_fare():
    entry = parse_fare_chart_entry(
        {
            "departureStation": "KTW",
            "arrivalStation": "EIN",
            "date": "2026-09-01T00:00:00",
            "price": {"amount": 0.0, "currencyCode": "PLN"},
            "priceType": "noData",
            "classOfService": None,
            "hasMacFlight": False,
        }
    )
    assert entry.price is None


def test_parse_network_and_destinations():
    network = parse_network(
        {
            "cities": [
                {
                    "iata": "TIA",
                    "mac": None,
                    "shortName": "Tirana",
                    "countryName": "Albania",
                    "countryCode": "AL",
                    "currencyCode": "ALL",
                    "latitude": 41.415,
                    "longitude": 19.72,
                    "aliases": ["Tirana"],
                    "categories": [7],
                    "rank": 1,
                    "isFakeStation": False,
                    "connections": [
                        {"iata": "AGP", "isDirectFlight": True, "isDomestic": False,
                         "isConnected": False, "isNew": False,
                         "operationStartDate": "2026-09-02T17:25:00"},
                        {"iata": "XXX", "isDirectFlight": False, "isDomestic": False,
                         "isConnected": True, "isNew": False,
                         "operationStartDate": None},
                    ],
                }
            ]
        }
    )
    station = network.station("tia")
    assert station is not None
    assert station.destinations() == ["AGP"]
    assert sorted(station.destinations(direct_only=False)) == ["AGP", "XXX"]


def test_parse_flight_dates_skips_junk():
    assert parse_flight_dates({"flightDates": ["2026-11-01T00:00:00", "nope"]}) == [
        datetime(2026, 11, 1)
    ]


def test_parse_flight_status():
    status = parse_flight_status(
        {
            "flightId": 45750213,
            "carrierCode": "W4",
            "flightNumber": 6201,
            "opSuffix": None,
            "originalDepartureAirport": "FCO",
            "departureAirport": "FCO",
            "originalArrivalAirport": "CHQ",
            "arrivalAirport": "CHQ",
            "operationDay": "2026-09-04T00:00:00",
        }
    )
    assert status.flight_number == 6201
    assert status.operation_day == datetime(2026, 9, 4)


def test_timetable_window_is_capped_at_the_api_limit():
    """A window wider than the endpoint accepts fails here, with a reason.

    Upstream answers `400 InvalidTimeDateRange` and says nothing about the
    limit, so the ceiling is enforced where the message can be useful.
    """
    base = dict(origin="BUD", destination="LTN", date_from=datetime(2026, 11, 1))
    TimetableSearch(
        **base, date_to=datetime(2026, 11, 1) + timedelta(days=MAX_TIMETABLE_WINDOW_DAYS)
    )
    with pytest.raises(PydanticValidationError, match="accepts at most"):
        TimetableSearch(
            **base,
            date_to=datetime(2026, 11, 1)
            + timedelta(days=MAX_TIMETABLE_WINDOW_DAYS + 1),
        )


def test_timetable_return_window_is_capped_too():
    with pytest.raises(PydanticValidationError, match="return_date_from"):
        TimetableSearch(
            origin="BUD",
            destination="LTN",
            date_from=datetime(2026, 11, 1),
            date_to=datetime(2026, 11, 10),
            return_date_from=datetime(2026, 11, 1),
            return_date_to=datetime(2027, 1, 1),
        )


def test_timetable_window_must_not_run_backwards():
    with pytest.raises(PydanticValidationError, match="must not be before"):
        TimetableSearch(
            origin="BUD",
            destination="LTN",
            date_from=datetime(2026, 11, 10),
            date_to=datetime(2026, 11, 1),
        )
