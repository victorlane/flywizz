"""
Public data models for Flywizz.

These describe what callers care about (a station, a fare, a network). The Wizz
Air wire format, its field names and its quirks, lives in :mod:`flywizz.wire`;
nothing in this module knows about it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator

__all__ = [
    "Price",
    "Connection",
    "Station",
    "Network",
    "TimetableEntry",
    "Departure",
    "FareChartEntry",
    "FlightStatus",
    "Country",
    "Culture",
    "DestinationFare",
    "TimetableSearch",
    "FareChartSearch",
    "AvailabilitySearch",
    "PriceType",
]

_IATA_PATTERN = re.compile(r"^[A-Z]{3}$")

PriceType = str  # "regular" or "wdc"


class Price(BaseModel):
    """A money amount as Wizz Air reports it.

    ``exchanged_amount`` is the SPA's client-side conversion hook and stays
    ``None`` for anonymous sessions.
    """

    amount: float
    currency: str
    exchanged_amount: Optional[float] = None
    exchanged_currency: Optional[str] = None


class Connection(BaseModel):
    """One reachable destination from a :class:`Station`."""

    iata: str
    is_direct: bool
    is_domestic: bool
    is_connected: bool
    is_new: bool
    operation_start_date: Optional[datetime] = None


class Station(BaseModel):
    """An airport in the Wizz Air network, from ``asset/map``.

    ``currency_code`` matters: fares for a route depart-priced in this currency
    when the station is the origin.
    """

    iata: str
    name: str
    country_code: str
    country_name: str
    currency_code: str
    latitude: float
    longitude: float
    mac: Optional[str] = None
    aliases: list[str] = []
    categories: list[int] = []
    rank: Optional[int] = None
    is_fake_station: bool = False
    connections: list[Connection] = []

    def destinations(self, direct_only: bool = True) -> list[str]:
        """IATA codes reachable from here."""
        return [
            c.iata for c in self.connections if c.is_direct or not direct_only
        ]


class Network(BaseModel):
    """The live Wizz Air route network."""

    stations: list[Station]

    def station(self, iata: str) -> Optional[Station]:
        code = iata.upper()
        return next((s for s in self.stations if s.iata == code), None)


class Departure(BaseModel):
    """One departure time on a timetable day."""

    departure: datetime
    is_cheapest_of_the_day: bool


class TimetableEntry(BaseModel):
    """One operating day for a route, with its cheapest fare.

    ``departures`` carries every departure that day, so a single timetable call
    answers both "when does it fly" and "what does the cheapest seat cost".
    """

    departure_station: str
    arrival_station: str
    departure_date: datetime
    price: Optional[Price] = None
    original_price: Optional[Price] = None
    price_type: Optional[str] = None
    departures: list[Departure] = []
    has_mac_flight: bool = False
    applied_coupon_code: Optional[str] = None


class FareChartEntry(BaseModel):
    """One day of the price strip from ``asset/farechart``."""

    departure_station: str
    arrival_station: str
    day: datetime
    price: Optional[Price] = None
    price_type: Optional[str] = None
    class_of_service: Optional[str] = None
    has_mac_flight: bool = False


class FlightStatus(BaseModel):
    """A single operating flight from ``asset/flightinformation``."""

    flight_id: int
    carrier_code: str
    flight_number: int
    departure_airport: str
    arrival_airport: str
    original_departure_airport: Optional[str] = None
    original_arrival_airport: Optional[str] = None
    operation_day: Optional[datetime] = None
    scheduled_departure: Optional[datetime] = None
    scheduled_arrival: Optional[datetime] = None
    op_suffix: Optional[str] = None


class Country(BaseModel):
    code: str
    name: str
    is_eu: bool
    is_schengen: bool
    phone_prefix: Optional[str] = None


class Culture(BaseModel):
    code: str
    name: str
    currencies: list[str] = []


class DestinationFare(BaseModel):
    """A reachable destination paired with its cheapest sampled fare, if any.

    ``price`` is ``None`` when the route is in the network but no priced
    inventory came back for the window.
    """

    station: Station
    price: Optional[Price] = None
    departure_date: Optional[datetime] = None


class _RouteSearch(BaseModel):
    origin: str
    destination: str
    adults: int = 1
    children: int = 0
    infants: int = 0
    price_type: PriceType = "regular"

    @field_validator("origin", "destination")
    @classmethod
    def validate_iata(cls, v: str) -> str:
        upper = v.upper()
        if not _IATA_PATTERN.fullmatch(upper):
            raise ValueError("Station code must be a 3-letter IATA code")
        return upper

    @field_validator("adults")
    @classmethod
    def validate_adults(cls, v: int) -> int:
        if v < 1:
            raise ValueError("At least one adult is required")
        return v


class TimetableSearch(_RouteSearch):
    """What a ``search/timetableV2`` call looks like in Flywizz terms."""

    date_from: datetime
    date_to: datetime
    return_date_from: Optional[datetime] = None
    return_date_to: Optional[datetime] = None

    @field_validator("date_to")
    @classmethod
    def validate_window(cls, v: datetime) -> datetime:
        return v


class FareChartSearch(_RouteSearch):
    """What an ``asset/farechart`` call looks like in Flywizz terms.

    ``day_interval`` is the half-window around ``date`` and must be at least 3;
    the API rejects smaller values with ``DayIntervalMustBeGreaterOrEqualTo3``.
    """

    date: datetime
    day_interval: int = 3

    @field_validator("day_interval")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if v < 3:
            raise ValueError("day_interval must be at least 3")
        return v


class AvailabilitySearch(_RouteSearch):
    """What a ``search/search`` call looks like in Flywizz terms.

    Only usable with browser-supplied Kasada headers; see
    :class:`flywizz.BotGateError`.
    """

    departure_date: datetime
    return_date: Optional[datetime] = None
    wdc: bool = True
    is_flight_change: bool = False
