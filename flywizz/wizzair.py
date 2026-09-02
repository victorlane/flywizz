"""
Wizz Air client. Coordinates the transport and parsers behind a small,
domain-friendly interface.

Endpoint method = (build path + body) -> transport.get_json / post_json ->
parser. Each method is a few lines because the depth lives behind the seams
(``flywizz.transport`` and ``flywizz.wire``).

Error contract: an empty list means "the API answered, but nothing matched".
Anything else raises. ``ValidationError`` carries the API's own validation codes,
``BotGateError`` means Kasada refused, and everything else is
``WizzairException``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator, Optional, Sequence

from .misc import (
    MAX_ARRIVAL_STATIONS,
    MAX_FLIGHT_DATES_WINDOW_DAYS,
    MAX_TIMETABLE_WINDOW_DAYS,
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
from .transport import Transport, WizzairException, WizzairTransport
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

logger = logging.getLogger("Flywizz")


def _batched(items: Sequence[str], size: int) -> Iterator[list[str]]:
    """Yield ``items`` in chunks of at most ``size``."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _check_flight_dates_window(date_from: datetime, date_to: datetime) -> None:
    """Reject a window the schedule endpoints will not answer for.

    Upstream says only ``InvalidTimeDateRange``, so the limit is named here.
    """
    span = (date_to.date() - date_from.date()).days
    if span < 0:
        raise ValueError("date_to must not be before date_from")
    if span > MAX_FLIGHT_DATES_WINDOW_DAYS:
        raise ValueError(
            f"date_from..date_to spans {span} days; the schedule endpoints "
            f"accept at most {MAX_FLIGHT_DATES_WINDOW_DAYS}"
        )


def _windows(
    date_from: datetime, date_to: datetime, weekend_length: int = 0
) -> list[tuple[datetime, datetime]]:
    """Split ``date_from``..``date_to`` into windows the timetable accepts.

    ``search/timetableV2`` caps a window at
    :data:`~flywizz.misc.MAX_TIMETABLE_WINDOW_DAYS` days. ``weekend_length``
    reserves room for the matching return leg, so a Friday at the end of one
    chunk still has its Sunday inside the same request.
    """
    span = MAX_TIMETABLE_WINDOW_DAYS - weekend_length
    out: list[tuple[datetime, datetime]] = []
    start = date_from
    while start <= date_to:
        end = min(start + timedelta(days=span), date_to)
        out.append((start, end))
        start = end + timedelta(days=1)
    return out


def _month_window(month: datetime) -> tuple[datetime, datetime]:
    """First and last day of the calendar month ``month`` falls in."""
    first = datetime(month.year, month.month, 1)
    if month.month == 12:
        next_first = datetime(month.year + 1, 1, 1)
    else:
        next_first = datetime(month.year, month.month + 1, 1)
    return first, next_first - timedelta(days=1)


class WizzAir:
    """Wizz Air flight-search client.

    :param transport: inject a custom :class:`Transport` (a
        :class:`CachingTransport` wrapping the default, or a fixture transport
        for tests). Defaults to a fresh :class:`WizzairTransport`.

    Prices come back in the departure station's local currency; Wizz Air has no
    server-side currency override. Read ``Station.currency_code`` from
    :meth:`get_network` if you need to know which one before you search.
    """

    def __init__(self, transport: Optional[Transport] = None):
        self.transport: Transport = transport or WizzairTransport()

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "WizzAir":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- network -----------------------------------------------------------

    def get_network(self, language: str = "en-gb") -> Network:
        """Every station Wizz Air serves and what each one connects to."""
        data = self.transport.get_json("asset/map", {"languageCode": language})
        try:
            return parse_network(data)
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected network shape: {exc}") from exc

    def get_destinations(self, origin: str, direct_only: bool = True) -> list[Station]:
        """Every station reachable from ``origin``."""
        network = self.get_network()
        station = network.station(origin)
        if station is None:
            return []
        reachable = set(station.destinations(direct_only=direct_only))
        return [s for s in network.stations if s.iata in reachable]

    def explore_by_country(self, origin: str) -> dict[str, list[Station]]:
        """Destinations from ``origin`` grouped by destination country code."""
        grouped: dict[str, list[Station]] = {}
        for station in self.get_destinations(origin):
            grouped.setdefault(station.country_code, []).append(station)
        return grouped

    def validate_route(self, origin: str, destination: str) -> bool:
        """True if Wizz Air currently flies ``origin`` to ``destination`` direct."""
        station = self.get_network().station(origin)
        if station is None:
            return False
        return destination.upper() in set(station.destinations())

    # --- schedules and fares -----------------------------------------------

    def get_flight_dates(
        self, origin: str, destination: str, date_from: datetime, date_to: datetime
    ) -> list[datetime]:
        """Dates the route operates in a window. No prices, very cheap."""
        params = serialize_flight_dates(origin, destination, date_from, date_to)
        return parse_flight_dates(self.transport.get_json("search/flightDates", params))

    def get_connecting_flight_dates(
        self, origin: str, destination: str, date_from: datetime, date_to: datetime
    ) -> list[FlightDate]:
        """Operating dates including connecting itineraries. No prices.

        Where :meth:`get_flight_dates` covers direct service only, this also
        answers for pairs the route map calls unreachable: ``KTW-LIS`` has no
        direct flight and :meth:`validate_route` returns ``False``, yet this
        finds operating days at one stop. On a route that is direct only some
        of the week, the two kinds of day come back interleaved.

        Each :class:`~flywizz.misc.FlightDate` carries ``stops``, so filter on
        ``is_direct`` if a connection will not do.

        :raises ValueError: if the window exceeds
            :data:`~flywizz.misc.MAX_FLIGHT_DATES_WINDOW_DAYS` days.
        """
        _check_flight_dates_window(date_from, date_to)
        # Same body shape as search/flightDates, but POSTed.
        body = serialize_flight_dates(origin, destination, date_from, date_to)
        data = self.transport.post_json("search/dohopFlightDates", body)
        try:
            return parse_connecting_flight_dates(data, destination)
        except (KeyError, ValueError) as exc:
            raise WizzairException(
                f"unexpected connecting-flight-dates shape: {exc}"
            ) from exc

    def get_flight_dates_multi(
        self,
        origin: str,
        destinations: Sequence[str],
        date_from: datetime,
        date_to: datetime,
    ) -> dict[str, list[datetime]]:
        """Operating dates for many destinations at once. No prices.

        The same schedule data as :meth:`get_flight_dates`, but for up to
        :data:`~flywizz.misc.MAX_ARRIVAL_STATIONS` destinations per request.
        Longer lists are batched, so the caller does not have to care: asking
        for 95 destinations costs 19 requests instead of 95.

        Every requested destination appears in the result. A route the airline
        does not operate in the window maps to an empty list, so there is no
        membership check to forget.

        :param destinations: IATA codes. Order is preserved in the result.
        :raises ValueError: if the window exceeds
            :data:`~flywizz.misc.MAX_FLIGHT_DATES_WINDOW_DAYS` days.
        """
        _check_flight_dates_window(date_from, date_to)
        wanted = [d.upper() for d in destinations]

        out: dict[str, list[datetime]] = {}
        for batch in _batched(wanted, MAX_ARRIVAL_STATIONS):
            data = self.transport.post_json(
                "search/FlightDatesMultiArrival",
                serialize_multi_arrival(origin, batch, date_from, date_to),
            )
            try:
                out.update(parse_multi_arrival(data, batch))
            except (KeyError, ValueError) as exc:
                raise WizzairException(
                    f"unexpected multi-arrival shape: {exc}"
                ) from exc
        return out

    def get_timetable(self, params: TimetableSearch) -> list[TimetableEntry]:
        """Cheapest fare per operating day, plus every departure time that day.

        The main priced surface reachable without a browser session.
        """
        data = self.transport.post_json("search/timetableV2", serialize_timetable(params))
        try:
            return [parse_timetable_entry(f) for f in data.get("outboundFlights", [])]
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected timetable shape: {exc}") from exc

    def get_return_timetable(
        self, params: TimetableSearch
    ) -> tuple[list[TimetableEntry], list[TimetableEntry]]:
        """Outbound and inbound timetables in one call.

        Requires ``return_date_from`` and ``return_date_to`` on ``params``.
        """
        if not (params.return_date_from and params.return_date_to):
            raise ValueError("return_date_from and return_date_to are required")
        data = self.transport.post_json("search/timetableV2", serialize_timetable(params))
        try:
            return (
                [parse_timetable_entry(f) for f in data.get("outboundFlights", [])],
                [parse_timetable_entry(f) for f in data.get("returnFlights", [])],
            )
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected timetable shape: {exc}") from exc

    def get_fare_chart(self, params: FareChartSearch) -> list[FareChartEntry]:
        """The price strip: cheapest price per day around a target date."""
        data = self.transport.post_json("asset/farechart", serialize_fare_chart(params))
        try:
            return [parse_fare_chart_entry(f) for f in data.get("outboundFlights", [])]
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected fare chart shape: {exc}") from exc

    def get_availability(self, params: AvailabilitySearch) -> dict:
        """Full availability with fare bundles and sell keys.

        Behind the Kasada bot gate: raises :class:`flywizz.BotGateError` unless
        the transport was built with ``kasada_headers`` from a live browser
        session. Returns the raw response, because the bundle structure is large,
        volatile, and only useful to callers driving the booking flow.
        """
        return self.transport.post_json("search/search", serialize_availability(params))

    # --- derived helpers ---------------------------------------------------

    def cheapest_in_month(
        self,
        origin: str,
        destination: str,
        month: datetime,
        price_type: str = "regular",
    ) -> Optional[TimetableEntry]:
        """The cheapest operating day for a route in one calendar month."""
        first, last = _month_window(month)
        entries = self.get_timetable(
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

    def cheapest_weekend(
        self,
        origin: str,
        destination: str,
        months_ahead: int = 3,
        weekend_length: int = 2,
    ) -> Optional[tuple[TimetableEntry, TimetableEntry]]:
        """Cheapest Fri-to-Sun (or Fri-to-Mon) return in the next months.

        Returns the (outbound, inbound) pair, or ``None`` if no priced weekend
        exists in the window.
        """
        if weekend_length not in (2, 3):
            raise ValueError("weekend_length must be 2 (Fri-Sun) or 3 (Fri-Mon)")
        now = datetime.now()
        end = now + timedelta(days=31 * months_ahead)

        # The endpoint caps a window well below three months, so walk the
        # range in chunks and keep the best pair across all of them.
        outbound: list[TimetableEntry] = []
        inbound: list[TimetableEntry] = []
        for start, stop in _windows(now, end, weekend_length):
            out_leg, back_leg = self.get_return_timetable(
                TimetableSearch(
                    origin=origin,
                    destination=destination,
                    date_from=start,
                    date_to=stop,
                    return_date_from=start,
                    return_date_to=stop + timedelta(days=weekend_length),
                )
            )
            outbound.extend(out_leg)
            inbound.extend(back_leg)

        inbound_by_day = {
            e.departure_date.date(): e for e in inbound if e.price is not None
        }

        best: Optional[tuple[TimetableEntry, TimetableEntry, float]] = None
        for out in outbound:
            if out.price is None or out.departure_date.weekday() != 4:  # 4 = Friday
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

    def explore_with_fares(
        self,
        origin: str,
        date_from: datetime,
        date_to: datetime,
        limit: Optional[int] = None,
    ) -> list[DestinationFare]:
        """Reachable destinations from ``origin``, each with its cheapest fare.

        Wizz Air has no "anywhere" search, so this fans out one timetable call
        per destination. That is N calls: pass ``limit`` while iterating, and
        wrap the transport in :class:`CachingTransport` so the network metadata
        is fetched once.

        Prefiltering with :meth:`get_flight_dates_multi` looks tempting and is
        not worth it: it costs ``N/5`` requests to save one per route that does
        not operate, so it only pays off above 20% dead routes. Measured on
        BUD, KTW and EIN it came to 18%, 22% and 11%, which is a wash at best.
        """
        destinations = self.get_destinations(origin)
        if limit is not None:
            destinations = destinations[:limit]

        out: list[DestinationFare] = []
        for station in destinations:
            try:
                entries = self.get_timetable(
                    TimetableSearch(
                        origin=origin,
                        destination=station.iata,
                        date_from=date_from,
                        date_to=date_to,
                    )
                )
            except WizzairException:
                logger.warning("no timetable for %s-%s", origin, station.iata)
                out.append(DestinationFare(station=station))
                continue
            priced = [e for e in entries if e.price is not None]
            cheapest = min(priced, key=lambda e: e.price.amount) if priced else None
            out.append(
                DestinationFare(
                    station=station,
                    price=cheapest.price if cheapest else None,
                    departure_date=cheapest.departure_date if cheapest else None,
                )
            )
        return out

    # --- reference data ----------------------------------------------------

    def get_flight_status(
        self, carrier_code: str, flight_number: str, date: Optional[datetime] = None
    ) -> list[FlightStatus]:
        """Live status for one flight number.

        The API takes exactly one filter, so route and flight number are mutually
        exclusive; this is the flight-number form. ``date`` is advisory: the
        endpoint answers with the operating days it knows about.
        """
        body = {"carrierCode": carrier_code.upper(), "flightNumber": str(flight_number)}
        if date is not None:
            body["date"] = date.date().isoformat()
        data = self.transport.post_json("asset/flightinformation", body)
        try:
            return [
                parse_flight_status(f)
                for f in data.get("flightInformationResponseItems", [])
            ]
        except (KeyError, ValueError) as exc:
            raise WizzairException(f"unexpected flight status shape: {exc}") from exc

    def get_currencies(self) -> list[str]:
        """ISO 4217 codes Wizz Air can price in."""
        return self.transport.get_json("asset/currencies").get("currencies", [])

    def get_countries(self) -> list[Country]:
        """Every country the booking form knows, with EU / Schengen flags."""
        data = self.transport.get_json("asset/country")
        return [parse_country(c) for c in data.get("countries", [])]

    def get_cultures(self) -> list[Culture]:
        """Supported site languages and the currencies each offers."""
        data = self.transport.get_json("asset/cultures")
        return [parse_culture(c) for c in data.get("cultures", [])]

    def get_service_fees(self, currencies: Optional[list[str]] = None) -> dict:
        """The published fee table (baggage, seats, changes) in the given currencies."""
        return self.transport.post_json(
            "asset/serviceFees", {"currencyCodes": currencies or ["EUR"]}
        )

    def get_wdc_prices(self) -> dict:
        """Wizz Discount Club membership tiers and guaranteed minimum discounts."""
        return self.transport.get_json("asset/wdcPrice")


__all__ = ["WizzAir"]
