"""
MCP server for Flywizz.

Exposes a small, agent-friendly slice of the Flywizz API over the Model Context
Protocol so LLM clients (Claude Desktop, Cursor, Claude Code, etc.) can search
Wizz Air fares from natural-language prompts like "what's the cheapest day in
November to fly Budapest to London".

Run via the ``flywizz-mcp`` console script registered in ``pyproject.toml``, or
directly with ``python -m flywizz.mcp_server``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

try:  # mcp >= 2.0 renamed FastMCP to MCPServer and moved the module
    from mcp.server.mcpserver import MCPServer as _Server
except ModuleNotFoundError:  # pragma: no cover - mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server

from .misc import FareChartEntry, FareChartSearch, TimetableEntry, TimetableSearch
from .transport import CachingTransport, WizzairTransport
from .wizzair import WizzAir

mcp = _Server("flywizz")

_client: Optional[WizzAir] = None


def _get_client() -> WizzAir:
    """Lazily build the WizzAir client so importing this module never hits the network.

    The transport is wrapped in a :class:`~flywizz.transport.CachingTransport` so
    the 650 KB network metadata is fetched once per process rather than on every
    ``explore_destinations`` call.
    """
    global _client
    if _client is None:
        _client = WizzAir(CachingTransport(WizzairTransport()))
    return _client


def _entry_to_dict(e: TimetableEntry) -> dict[str, Any]:
    """Trim a TimetableEntry to the fields an agent actually needs.

    Every departure time that day is kept, because "is there an evening flight"
    is a question agents get asked constantly, but the nested price objects are
    flattened to keep the response compact.
    """
    return {
        "day": e.departure_date.date().isoformat(),
        "from": e.departure_station,
        "to": e.arrival_station,
        "price": e.price.amount if e.price else None,
        "currency": e.price.currency if e.price else None,
        "original_price": e.original_price.amount if e.original_price else None,
        "departures": [d.departure.isoformat() for d in e.departures],
        "cheapest_departure": next(
            (d.departure.isoformat() for d in e.departures if d.is_cheapest_of_the_day),
            None,
        ),
    }


def _chart_to_dict(e: FareChartEntry) -> dict[str, Any]:
    return {
        "day": e.day.date().isoformat(),
        "from": e.departure_station,
        "to": e.arrival_station,
        "price": e.price.amount if e.price else None,
        "currency": e.price.currency if e.price else None,
        "class_of_service": e.class_of_service,
    }


@mcp.tool()
def find_fares(
    origin: str,
    destination: str,
    date_from: str,
    date_to: str,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    wizz_discount_club: bool = False,
) -> list[dict[str, Any]]:
    """Cheapest Wizz Air fare per day for one route across a date window.

    ``origin`` and ``destination`` are 3-letter IATA codes (e.g. ``BUD``,
    ``LTN``). Dates are ISO format (``YYYY-MM-DD``) and bound a departure
    window; every operating day inside it comes back with its cheapest fare and
    every departure time that day.

    Prices are in the *departure station's* local currency (BUD quotes in HUF,
    LTN in GBP). Wizz Air has no currency override, so always report the
    ``currency`` field alongside the amount.

    Set ``wizz_discount_club=True`` to price as a Discount Club member.
    """
    entries = _get_client().get_timetable(
        TimetableSearch(
            origin=origin,
            destination=destination,
            date_from=datetime.fromisoformat(date_from),
            date_to=datetime.fromisoformat(date_to),
            adults=adults,
            children=children,
            infants=infants,
            price_type="wdc" if wizz_discount_club else "regular",
        )
    )
    return [_entry_to_dict(e) for e in entries]


@mcp.tool()
def cheapest_day(
    origin: str,
    destination: str,
    month: str,
    wizz_discount_club: bool = False,
) -> Optional[dict[str, Any]]:
    """The single cheapest operating day for a route in one calendar month.

    ``month`` is any ISO date inside the month of interest (``YYYY-MM-01`` is
    the obvious choice). Returns ``None`` when no priced inventory exists that
    month. Powers "what's the cheapest day in November to fly BUD to LTN".
    """
    entry = _get_client().cheapest_in_month(
        origin=origin,
        destination=destination,
        month=datetime.fromisoformat(month),
        price_type="wdc" if wizz_discount_club else "regular",
    )
    return _entry_to_dict(entry) if entry else None


@mcp.tool()
def price_around(
    origin: str,
    destination: str,
    date: str,
    day_interval: int = 3,
) -> list[dict[str, Any]]:
    """The price strip: cheapest fare for each day around a target date.

    ``day_interval`` is the half-window and must be at least 3, so the default
    returns seven days centred on ``date``. Cheaper and narrower than
    :func:`find_fares`; use it to answer "is it cheaper a day either side".
    """
    entries = _get_client().get_fare_chart(
        FareChartSearch(
            origin=origin,
            destination=destination,
            date=datetime.fromisoformat(date),
            day_interval=day_interval,
        )
    )
    return [_chart_to_dict(e) for e in entries]


@mcp.tool()
def explore_destinations(origin: str) -> dict[str, list[dict[str, str]]]:
    """Every destination Wizz Air flies to from ``origin``, grouped by country.

    Returns ``{country_code: [{iata, name, currency}]}``. Country codes are
    uppercase ISO2. No fare lookup, just the network, so this is cheap and
    answers "what countries can I reach from X" or "does Wizz fly X to Y".
    """
    grouped = _get_client().explore_by_country(origin)
    return {
        country: [
            {"iata": s.iata, "name": s.name, "currency": s.currency_code}
            for s in stations
        ]
        for country, stations in grouped.items()
    }


@mcp.tool()
def flight_status(
    carrier_code: str,
    flight_number: str,
    date: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Live schedule and status for one Wizz Air flight number.

    ``carrier_code`` is the AOC prefix: ``W6`` (Hungary), ``W4`` (Malta), ``W9``
    (UK). ``flight_number`` is the numeric part alone (e.g. ``6201``). ``date``
    is advisory; the endpoint answers with the operating days it knows about.
    """
    statuses = _get_client().get_flight_status(
        carrier_code=carrier_code,
        flight_number=flight_number,
        date=datetime.fromisoformat(date) if date else None,
    )
    return [
        {
            "carrier_code": s.carrier_code,
            "flight_number": s.flight_number,
            "from": s.departure_airport,
            "to": s.arrival_airport,
            "operation_day": s.operation_day.date().isoformat()
            if s.operation_day
            else None,
            "scheduled_departure": s.scheduled_departure.isoformat()
            if s.scheduled_departure
            else None,
            "scheduled_arrival": s.scheduled_arrival.isoformat()
            if s.scheduled_arrival
            else None,
            "diverted": s.departure_airport != (s.original_departure_airport or s.departure_airport)
            or s.arrival_airport != (s.original_arrival_airport or s.arrival_airport),
        }
        for s in statuses
    ]


def main() -> None:
    """Entry point for the ``flywizz-mcp`` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
