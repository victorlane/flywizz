"""
Translation between Flywizz's public models and Wizz Air's wire format.

The Wizz Air field names (``departureStation``, ``flightList``, ``dayInterval``)
and quirks (uppercase ISO2 country codes, bare dates on timetable but datetimes
on availability, currency fixed to the departure station) live here and nowhere
else. ``flywizz.misc`` describes what callers care about; this module describes
what the API expects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .misc import (
    AvailabilitySearch,
    Connection,
    Country,
    Culture,
    Departure,
    FareChartEntry,
    FareChartSearch,
    FlightStatus,
    Network,
    Price,
    Station,
    TimetableEntry,
    TimetableSearch,
)


def _date(value: datetime) -> str:
    return value.date().isoformat()


def _datetime_at_midnight(value: datetime) -> str:
    return f"{value.date().isoformat()}T00:00:00"


def _parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def serialize_timetable(params: TimetableSearch) -> Dict[str, Any]:
    """Render a timetable search into the ``search/timetableV2`` body."""
    flight_list = [
        {
            "departureStation": params.origin,
            "arrivalStation": params.destination,
            "from": _date(params.date_from),
            "to": _date(params.date_to),
        }
    ]
    if params.return_date_from and params.return_date_to:
        flight_list.append(
            {
                "departureStation": params.destination,
                "arrivalStation": params.origin,
                "from": _date(params.return_date_from),
                "to": _date(params.return_date_to),
            }
        )
    return {
        "flightList": flight_list,
        "priceType": params.price_type,
        "adultCount": params.adults,
        "childCount": params.children,
        "infantCount": params.infants,
    }


def serialize_fare_chart(params: FareChartSearch) -> Dict[str, Any]:
    """Render a fare-chart search into the ``asset/farechart`` body."""
    return {
        "flightList": [
            {
                "departureStation": params.origin,
                "arrivalStation": params.destination,
                "date": _date(params.date),
            }
        ],
        "priceType": params.price_type,
        "adultCount": params.adults,
        "childCount": params.children,
        "infantCount": params.infants,
        "dayInterval": params.day_interval,
    }


def serialize_availability(params: AvailabilitySearch) -> Dict[str, Any]:
    """Render an availability search into the ``search/search`` body.

    Availability wants full ISO datetimes at midnight; timetable and fare chart
    want bare dates. The API is not consistent, so the difference lives here.
    """
    flight_list = [
        {
            "departureStation": params.origin,
            "arrivalStation": params.destination,
            "departureDate": _datetime_at_midnight(params.departure_date),
        }
    ]
    if params.return_date:
        flight_list.append(
            {
                "departureStation": params.destination,
                "arrivalStation": params.origin,
                "departureDate": _datetime_at_midnight(params.return_date),
            }
        )
    return {
        "isFlightChange": params.is_flight_change,
        "flightList": flight_list,
        "adultCount": params.adults,
        "childCount": params.children,
        "infantCount": params.infants,
        "wdc": params.wdc,
    }


def serialize_flight_dates(
    origin: str, destination: str, date_from: datetime, date_to: datetime
) -> Dict[str, Any]:
    return {
        "departureStation": origin.upper(),
        "arrivalStation": destination.upper(),
        "from": _date(date_from),
        "to": _date(date_to),
    }


def parse_price(raw: Optional[Dict[str, Any]]) -> Optional[Price]:
    if not raw or raw.get("amount") is None:
        return None
    return Price(
        amount=raw["amount"],
        currency=raw["currencyCode"],
        exchanged_amount=raw.get("exchangedAmount"),
        exchanged_currency=raw.get("exchangedCurrencyCode"),
    )


def parse_connection(raw: Dict[str, Any]) -> Connection:
    return Connection(
        iata=raw["iata"],
        is_direct=bool(raw.get("isDirectFlight", False)),
        is_domestic=bool(raw.get("isDomestic", False)),
        is_connected=bool(raw.get("isConnected", False)),
        is_new=bool(raw.get("isNew", False)),
        operation_start_date=_parse_dt(raw.get("operationStartDate")),
    )


def parse_station(raw: Dict[str, Any]) -> Station:
    return Station(
        iata=raw["iata"],
        name=raw["shortName"],
        country_code=raw["countryCode"],
        country_name=raw["countryName"],
        currency_code=raw["currencyCode"],
        latitude=raw["latitude"],
        longitude=raw["longitude"],
        mac=raw.get("mac"),
        aliases=raw.get("aliases") or [],
        categories=raw.get("categories") or [],
        rank=raw.get("rank"),
        is_fake_station=bool(raw.get("isFakeStation", False)),
        connections=[parse_connection(c) for c in raw.get("connections", [])],
    )


def parse_network(raw: Dict[str, Any]) -> Network:
    return Network(stations=[parse_station(s) for s in raw.get("cities", [])])


def parse_timetable_entry(raw: Dict[str, Any]) -> TimetableEntry:
    return TimetableEntry(
        departure_station=raw["departureStation"],
        arrival_station=raw["arrivalStation"],
        departure_date=datetime.fromisoformat(raw["departureDate"]),
        price=parse_price(raw.get("price")),
        original_price=parse_price(raw.get("originalPrice")),
        price_type=raw.get("priceType"),
        departures=[
            Departure(
                departure=datetime.fromisoformat(d["date"]),
                is_cheapest_of_the_day=bool(d.get("isCheapestOfTheDay", False)),
            )
            for d in raw.get("departureDates", [])
        ],
        has_mac_flight=bool(raw.get("hasMacFlight", False)),
        applied_coupon_code=raw.get("appliedCouponCode"),
    )


def parse_fare_chart_entry(raw: Dict[str, Any]) -> FareChartEntry:
    return FareChartEntry(
        departure_station=raw["departureStation"],
        arrival_station=raw["arrivalStation"],
        day=datetime.fromisoformat(raw["date"]),
        price=parse_price(raw.get("price")),
        price_type=raw.get("priceType"),
        class_of_service=raw.get("classOfService"),
        has_mac_flight=bool(raw.get("hasMacFlight", False)),
    )


def parse_flight_status(raw: Dict[str, Any]) -> FlightStatus:
    return FlightStatus(
        flight_id=raw["flightId"],
        carrier_code=raw["carrierCode"],
        flight_number=raw["flightNumber"],
        departure_airport=raw["departureAirport"],
        arrival_airport=raw["arrivalAirport"],
        original_departure_airport=raw.get("originalDepartureAirport"),
        original_arrival_airport=raw.get("originalArrivalAirport"),
        operation_day=_parse_dt(raw.get("operationDay")),
        scheduled_departure=_parse_dt(raw.get("scheduledDepartureTime")),
        scheduled_arrival=_parse_dt(raw.get("scheduledArrivalTime")),
        op_suffix=raw.get("opSuffix"),
    )


def parse_country(raw: Dict[str, Any]) -> Country:
    return Country(
        code=raw["code"],
        name=raw["name"],
        is_eu=bool(raw.get("isEu", False)),
        is_schengen=bool(raw.get("isSchengen", False)),
        phone_prefix=raw.get("phonePrefix"),
    )


def parse_culture(raw: Dict[str, Any]) -> Culture:
    return Culture(
        code=raw["code"],
        name=raw["name"],
        currencies=raw.get("currencyCode") or [],
    )


def parse_flight_dates(raw: Dict[str, Any]) -> list[datetime]:
    out: list[datetime] = []
    for value in raw.get("flightDates", []):
        parsed = _parse_dt(value)
        if parsed is not None:
            out.append(parsed)
    return out
