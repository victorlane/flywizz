"""
Async mirror of :class:`flywizz.WizzAir`.

Every public method on ``WizzAir`` exists here with the same signature. Keep the
two in lockstep: add to both, or neither.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .misc import (
    MAX_ARRIVAL_STATIONS,
    AvailabilitySearch,
    Country,
    Culture,
    DestinationFare,
    FareChartEntry,
    FareChartSearch,
    FlightDate,
    FlightStatus,
    Network,
    Station,
    TimetableEntry,
    TimetableSearch,
)
from .transport import AsyncTransport, AsyncWizzairTransport, WizzairException
from .wire import (
    parse_connecting_flight_dates,
    parse_country,
    parse_culture,
    parse_fare_chart_entry,
    parse_flight_dates,
    parse_flight_status,
    parse_multi_arrival,
    parse_network,
    parse_timetable_entry,
    serialize_availability,
    serialize_fare_chart,
    serialize_flight_dates,
    serialize_multi_arrival,
    serialize_timetable,
)
from .wizzair import (
    _batched,
    _check_flight_dates_window,
    _month_window,
    _windows,
)

logger = logging.getLogger("Flywizz")


class AsyncWizzAir:
    """Async Wizz Air flight-search client.

    :param transport: inject a custom :class:`AsyncTransport`. Defaults to a
        fresh :class:`AsyncWizzairTransport`.
    """

    def __init__(self, transport: Optional[AsyncTransport] = None):
        self.transport: AsyncTransport = transport or AsyncWizzairTransport()

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def __aenter__(self) -> "AsyncWizzAir":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # --- network -----------------------------------------------------------

    async def get_network(self, language: str = "en-gb") -> Network:
        """Every station Wizz Air serves and what each one connects to."""
        data = await self.transport.get_json("asset/map", {"languageCode": language})
        try:
            return parse_network(data)
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected network shape: {exc}") from exc

    async def get_destinations(
        self, origin: str, direct_only: bool = True
    ) -> list[Station]:
        """Every station reachable from ``origin``."""
        network = await self.get_network()
        station = network.station(origin)
        if station is None:
            return []
        reachable = set(station.destinations(direct_only=direct_only))
        return [s for s in network.stations if s.iata in reachable]

    async def explore_by_country(self, origin: str) -> dict[str, list[Station]]:
        """Destinations from ``origin`` grouped by destination country code."""
        grouped: dict[str, list[Station]] = {}
        for station in await self.get_destinations(origin):
            grouped.setdefault(station.country_code, []).append(station)
        return grouped

    async def validate_route(self, origin: str, destination: str) -> bool:
        """True if Wizz Air currently flies ``origin`` to ``destination`` direct."""
        network = await self.get_network()
        station = network.station(origin)
        if station is None:
            return False
        return destination.upper() in set(station.destinations())

    # --- schedules and fares -----------------------------------------------

    async def get_flight_dates(
        self, origin: str, destination: str, date_from: datetime, date_to: datetime
    ) -> list[datetime]:
        """Dates the route operates in a window. No prices, very cheap."""
        params = serialize_flight_dates(origin, destination, date_from, date_to)
        data = await self.transport.get_json("search/flightDates", params)
        return parse_flight_dates(data)

    async def get_connecting_flight_dates(
        self, origin: str, destination: str, date_from: datetime, date_to: datetime
    ) -> list[FlightDate]:
        """Operating dates including connecting itineraries. No prices.

        See :meth:`flywizz.WizzAir.get_connecting_flight_dates`.
        """
        _check_flight_dates_window(date_from, date_to)
        body = serialize_flight_dates(origin, destination, date_from, date_to)
        data = await self.transport.post_json("search/dohopFlightDates", body)
        try:
            return parse_connecting_flight_dates(data, destination)
        except (KeyError, ValueError) as exc:
            raise WizzairException(
                f"unexpected connecting-flight-dates shape: {exc}"
            ) from exc

    async def get_flight_dates_multi(
        self,
        origin: str,
        destinations: Sequence[str],
        date_from: datetime,
        date_to: datetime,
    ) -> dict[str, list[datetime]]:
        """Operating dates for many destinations at once. No prices.

        The batches the sync client walks in sequence are fanned out
        concurrently here. See :meth:`flywizz.WizzAir.get_flight_dates_multi`.
        """
        _check_flight_dates_window(date_from, date_to)
        wanted = [d.upper() for d in destinations]
        batches = list(_batched(wanted, MAX_ARRIVAL_STATIONS))

        responses = await asyncio.gather(
            *(
                self.transport.post_json(
                    "search/FlightDatesMultiArrival",
                    serialize_multi_arrival(origin, batch, date_from, date_to),
                )
                for batch in batches
            )
        )

        out: dict[str, list[datetime]] = {}
        for batch, data in zip(batches, responses, strict=True):
            try:
                out.update(parse_multi_arrival(data, batch))
            except (KeyError, ValueError) as exc:
                raise WizzairException(
                    f"unexpected multi-arrival shape: {exc}"
                ) from exc
        return out

    async def get_timetable(self, params: TimetableSearch) -> list[TimetableEntry]:
        """Cheapest fare per operating day, plus every departure time that day."""
        data = await self.transport.post_json(
            "search/timetableV2", serialize_timetable(params)
        )
        try:
            return [parse_timetable_entry(f) for f in data.get("outboundFlights", [])]
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected timetable shape: {exc}") from exc

    async def get_return_timetable(
        self, params: TimetableSearch
    ) -> tuple[list[TimetableEntry], list[TimetableEntry]]:
        """Outbound and inbound timetables in one call."""
        if not (params.return_date_from and params.return_date_to):
            raise ValueError("return_date_from and return_date_to are required")
        data = await self.transport.post_json(
            "search/timetableV2", serialize_timetable(params)
        )
        try:
            return (
                [parse_timetable_entry(f) for f in data.get("outboundFlights", [])],
                [parse_timetable_entry(f) for f in data.get("returnFlights", [])],
            )
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected timetable shape: {exc}") from exc

    async def get_fare_chart(self, params: FareChartSearch) -> list[FareChartEntry]:
        """The price strip: cheapest price per day around a target date."""
        data = await self.transport.post_json(
            "asset/farechart", serialize_fare_chart(params)
        )
        try:
            return [parse_fare_chart_entry(f) for f in data.get("outboundFlights", [])]
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected fare chart shape: {exc}") from exc

    async def get_availability(self, params: AvailabilitySearch) -> dict:
        """Full availability with fare bundles and sell keys. Kasada-gated."""
        return await self.transport.post_json(
            "search/search", serialize_availability(params)
        )

    # --- derived helpers ---------------------------------------------------

    async def cheapest_in_month(
        self,
        origin: str,
        destination: str,
        month: datetime,
        price_type: str = "regular",
    ) -> Optional[TimetableEntry]:
        """The cheapest operating day for a route in one calendar month."""
        first, last = _month_window(month)
        entries = await self.get_timetable(
            TimetableSearch(
                origin=origin,
                destination=destination,
                date_from=first,
                date_to=last,
                price_type=price_type,
            )
        )
        priced = [e for e in entries if e.price is not None]
        return min(priced, key=lambda e: e.price.amount) if priced else None

    async def cheapest_weekend(
        self,
        origin: str,
        destination: str,
        months_ahead: int = 3,
        weekend_length: int = 2,
    ) -> Optional[tuple[TimetableEntry, TimetableEntry]]:
        """Cheapest Fri-to-Sun (or Fri-to-Mon) return in the next months."""
        if weekend_length not in (2, 3):
            raise ValueError("weekend_length must be 2 (Fri-Sun) or 3 (Fri-Mon)")
        now = datetime.now()
        end = now + timedelta(days=31 * months_ahead)

        # The endpoint caps a window well below three months, so fan the
        # chunks out concurrently and keep the best pair across all of them.
        legs = await asyncio.gather(
            *(
                self.get_return_timetable(
                    TimetableSearch(
                        origin=origin,
                        destination=destination,
                        date_from=start,
                        date_to=stop,
                        return_date_from=start,
                        return_date_to=stop + timedelta(days=weekend_length),
                    )
                )
                for start, stop in _windows(now, end, weekend_length)
            )
        )
        outbound = [e for out_leg, _ in legs for e in out_leg]
        inbound = [e for _, back_leg in legs for e in back_leg]

        inbound_by_day = {
            e.departure_date.date(): e for e in inbound if e.price is not None
        }

        best: Optional[tuple[TimetableEntry, TimetableEntry, float]] = None
        for out in outbound:
            if out.price is None or out.departure_date.weekday() != 4:
                continue
            back = inbound_by_day.get(
                (out.departure_date + timedelta(days=weekend_length)).date()
            )
            if back is None or back.price is None:
                continue
            total = out.price.amount + back.price.amount
            if best is None or total < best[2]:
                best = (out, back, total)
        return (best[0], best[1]) if best else None

    async def explore_with_fares(
        self,
        origin: str,
        date_from: datetime,
        date_to: datetime,
        limit: Optional[int] = None,
        concurrency: int = 5,
    ) -> list[DestinationFare]:
        """Reachable destinations from ``origin``, each with its cheapest fare.

        One timetable call per destination, fanned out ``concurrency`` at a time.
        See :meth:`flywizz.WizzAir.explore_with_fares` for why this does not
        prefilter with :meth:`get_flight_dates_multi`.
        """
        destinations = await self.get_destinations(origin)
        if limit is not None:
            destinations = destinations[:limit]
        semaphore = asyncio.Semaphore(concurrency)

        async def probe(station: Station) -> DestinationFare:
            async with semaphore:
                try:
                    entries = await self.get_timetable(
                        TimetableSearch(
                            origin=origin,
                            destination=station.iata,
                            date_from=date_from,
                            date_to=date_to,
                        )
                    )
                except WizzairException:
                    logger.warning("no timetable for %s-%s", origin, station.iata)
                    return DestinationFare(station=station)
            priced = [e for e in entries if e.price is not None]
            cheapest = min(priced, key=lambda e: e.price.amount) if priced else None
            return DestinationFare(
                station=station,
                price=cheapest.price if cheapest else None,
                departure_date=cheapest.departure_date if cheapest else None,
            )

        return list(await asyncio.gather(*(probe(s) for s in destinations)))

    # --- reference data ----------------------------------------------------

    async def get_flight_status(
        self, carrier_code: str, flight_number: str, date: Optional[datetime] = None
    ) -> list[FlightStatus]:
        """Live status for one flight number."""
        body = {"carrierCode": carrier_code.upper(), "flightNumber": str(flight_number)}
        if date is not None:
            body["date"] = date.date().isoformat()
        data = await self.transport.post_json("asset/flightinformation", body)
        try:
            return [
                parse_flight_status(f)
                for f in data.get("flightInformationResponseItems", [])
            ]
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected flight status shape: {exc}") from exc

    async def get_currencies(self) -> list[str]:
        """ISO 4217 codes Wizz Air can price in."""
        data = await self.transport.get_json("asset/currencies")
        return data.get("currencies", [])

    async def get_countries(self) -> list[Country]:
        """Every country the booking form knows, with EU / Schengen flags."""
        data = await self.transport.get_json("asset/country")
        return [parse_country(c) for c in data.get("countries", [])]

    async def get_cultures(self) -> list[Culture]:
        """Supported site languages and the currencies each offers."""
        data = await self.transport.get_json("asset/cultures")
        return [parse_culture(c) for c in data.get("cultures", [])]

    async def get_service_fees(self, currencies: Optional[list[str]] = None) -> dict:
        """The published fee table in the given currencies."""
        return await self.transport.post_json(
            "asset/serviceFees", {"currencyCodes": currencies or ["EUR"]}
        )

    async def get_wdc_prices(self) -> dict:
        """Wizz Discount Club membership tiers and guaranteed minimum discounts."""
        return await self.transport.get_json("asset/wdcPrice")


__all__ = ["AsyncWizzAir"]
