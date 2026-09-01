#!/usr/bin/env python3
"""Live probe of the Wizz Air endpoints Flywizz depends on.

Wizz Air's API has no contract, so shape changes break us silently. This
script (run daily by ``.github/workflows/api-health.yml``) exercises the
network, schedule, fare and reference endpoints through Flywizz's own
transport and asserts that the shapes ``flywizz.wire`` expects still hold.

On any drift it writes a markdown report (``--report``) describing the
difference and exits 1, so the workflow can open an ``endpoint`` issue
pointing at ``flywizz/wire.py`` and ``docs/internal-api-spec.md``.

Wizz Air fronts the API with Kasada and CloudFront, either of which can turn
a CI runner's IP away with a blanket 429 that has nothing to do with the
response shape. That is not drift, so the probe retries the whole run on a
fresh session and, if every attempt is still blocked, exits ``BLOCKED_EXIT``
(2) without writing a report — the workflow turns that into a warning
instead of an issue.

``search/search`` is expected to be blocked: it sits permanently behind the
bot gate. The probe asserts that it *stays* blocked and reports it as drift
if the gate ever lifts, since that would open up a much richer surface.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import List

from flywizz.misc import AvailabilitySearch, FareChartSearch, TimetableSearch
from flywizz.transport import (
    BotGateError,
    CachingTransport,
    WizzairException,
    WizzairTransport,
)
from flywizz.wizzair import WizzAir

# The probe is blocked, not broken, when the edge turns the runner away.
BLOCKED_EXIT = 2
# Each attempt builds a fresh transport, so it gets a new User-Agent, a new
# cookie warm-up and a new verification token.
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 20.0

ORIGIN = "BUD"
DESTINATION = "LTN"
# Probe a window far enough out that the route is bookable but near enough
# that the schedule is published and priced.
WINDOW_DAYS_AHEAD = 30
WINDOW_LENGTH_DAYS = 30

MIN_STATIONS = 100
# asset/map is the only source of truth for the network; these are the fields
# parse_station() reads unconditionally.
STATION_REQUIRED_FIELDS = (
    "iata",
    "shortName",
    "countryName",
    "countryCode",
    "currencyCode",
    "latitude",
    "longitude",
    "connections",
)
CONNECTION_REQUIRED_FIELDS = ("iata", "isDirectFlight")

# Endpoints that are documented as gated. If one stops answering 429 the spec
# is wrong and we want to know.
GATED_PATH = "search/search"


class Blocked(Exception):
    """The edge refused this runner, so no shape could be checked."""


def check_network(client: WizzAir) -> List[str]:
    """Verify asset/map's shape and that the sample origin still has routes."""
    failures: List[str] = []
    raw = client.transport.get_json("asset/map", {"languageCode": "en-gb"})

    if not isinstance(raw, dict):
        return [f"asset/map: expected a JSON object, got `{type(raw).__name__}`"]

    cities = raw.get("cities")
    if not isinstance(cities, list):
        return ["asset/map: missing top-level `cities` list"]
    if len(cities) < MIN_STATIONS:
        failures.append(
            f"asset/map: only {len(cities)} stations, expected at least {MIN_STATIONS}"
        )

    sample = next((c for c in cities if c.get("iata") == ORIGIN), None)
    if sample is None:
        failures.append(
            f"asset/map: sample station `{ORIGIN}` not found among {len(cities)} cities"
        )
    else:
        missing = [f for f in STATION_REQUIRED_FIELDS if f not in sample]
        if missing:
            failures.append(
                f"asset/map: station `{ORIGIN}` is missing fields `{missing}` "
                f"(got `{sorted(sample.keys())}`)"
            )
        connections = sample.get("connections") or []
        if not connections:
            failures.append(f"asset/map: station `{ORIGIN}` has no connections")
        else:
            missing_conn = [
                f for f in CONNECTION_REQUIRED_FIELDS if f not in connections[0]
            ]
            if missing_conn:
                failures.append(
                    f"asset/map: connection on `{ORIGIN}` is missing "
                    f"`{missing_conn}` (got `{sorted(connections[0].keys())}`)"
                )

    if failures:
        return failures

    try:
        network = client.get_network()
    except Exception as exc:  # noqa: BLE001 - report any parse drift
        return [f"asset/map: `parse_network` raised `{exc!r}`"]

    station = network.station(ORIGIN)
    if station is None or not station.destinations():
        failures.append(
            f"asset/map: parsed network has no direct destinations for `{ORIGIN}`"
        )
    return failures


def check_flight_dates(client: WizzAir) -> List[str]:
    """Verify search/flightDates still answers with a list of ISO datetimes."""
    start, end = _window()
    dates = client.get_flight_dates(ORIGIN, DESTINATION, start, end)
    if not dates:
        return [
            f"search/flightDates: no operating dates for `{ORIGIN}-{DESTINATION}` "
            f"between {start.date()} and {end.date()}"
        ]
    return []


def check_timetable(client: WizzAir) -> List[str]:
    """Verify search/timetableV2's shape: the main priced surface."""
    failures: List[str] = []
    start, end = _window()
    entries = client.get_timetable(
        TimetableSearch(
            origin=ORIGIN, destination=DESTINATION, date_from=start, date_to=end
        )
    )
    if not entries:
        return [
            f"search/timetableV2: no operating days for `{ORIGIN}-{DESTINATION}` "
            f"between {start.date()} and {end.date()}"
        ]

    priced = [e for e in entries if e.price is not None]
    if not priced:
        failures.append(
            f"search/timetableV2: {len(entries)} days returned but none carried a price"
        )
        return failures

    station = client.get_network().station(ORIGIN)
    expected = station.currency_code if station else None
    actual = priced[0].price.currency
    if expected and actual != expected:
        failures.append(
            f"search/timetableV2: prices came back in `{actual}` but `{ORIGIN}` "
            f"is an `{expected}` station — the currency rule in "
            f"`docs/internal-api-spec.md` no longer holds"
        )
    if not any(e.departures for e in entries):
        failures.append(
            "search/timetableV2: no day carried a `departureDates` list; the "
            "timetable half of the response is gone"
        )
    return failures


def check_fare_chart(client: WizzAir) -> List[str]:
    """Verify asset/farechart still returns a symmetric day strip."""
    start, _ = _window()
    entries = client.get_fare_chart(
        FareChartSearch(
            origin=ORIGIN, destination=DESTINATION, date=start, day_interval=3
        )
    )
    if len(entries) < 3:
        return [
            f"asset/farechart: strip has {len(entries)} days for `dayInterval=3`, "
            f"expected at least 3"
        ]
    return []


def check_reference_data(client: WizzAir) -> List[str]:
    """Verify the static asset endpoints still answer with populated lists."""
    failures: List[str] = []
    for name, fetch in (
        ("asset/currencies", client.get_currencies),
        ("asset/country", client.get_countries),
        ("asset/cultures", client.get_cultures),
    ):
        try:
            values = fetch()
        except Exception as exc:  # noqa: BLE001 - a dead endpoint is a finding
            failures.append(f"{name}: raised `{exc!r}`")
            continue
        if not values:
            failures.append(f"{name}: came back empty")
    return failures


def check_bot_gate(client: WizzAir) -> List[str]:
    """Assert search/search is still gated, and report it if the gate lifts."""
    start, _ = _window()
    try:
        client.get_availability(
            AvailabilitySearch(
                origin=ORIGIN, destination=DESTINATION, departure_date=start
            )
        )
    except BotGateError:
        return []
    except WizzairException as exc:
        return [
            f"{GATED_PATH}: expected a 429 bot gate but got `{exc}` — the gate "
            f"behaviour documented in `docs/internal-api-spec.md` has changed"
        ]
    return [
        f"{GATED_PATH}: answered without Kasada headers. The bot gate appears to "
        f"be gone, which unlocks fare bundles and sell keys — update "
        f"`docs/internal-api-spec.md` and consider exposing them"
    ]


def _window() -> tuple[datetime, datetime]:
    start = datetime.now() + timedelta(days=WINDOW_DAYS_AHEAD)
    return start, start + timedelta(days=WINDOW_LENGTH_DAYS)


def _is_blocked(exc: BaseException, path: str) -> bool:
    """True when ``exc`` is the edge turning us away rather than shape drift.

    A ``BotGateError`` on a path we *expect* to be gated is a finding, not a
    block, so the gated path is excluded here.
    """
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, BotGateError) and path != GATED_PATH:
            return True
        current = current.__cause__ or current.__context__
    return False


def _run_once() -> List[str]:
    """One full pass over every check on a fresh session.

    :raises Blocked: if any check was turned away by the edge; the remaining
        checks are skipped because their verdicts would be meaningless.
    """
    failures: List[str] = []
    transport = WizzairTransport()
    client = WizzAir(CachingTransport(transport))
    try:
        for name, check in (
            ("asset/map", check_network),
            ("search/flightDates", check_flight_dates),
            ("search/timetableV2", check_timetable),
            ("asset/farechart", check_fare_chart),
            ("reference data", check_reference_data),
            (GATED_PATH, check_bot_gate),
        ):
            try:
                failures.extend(check(client))
            except Exception as exc:  # noqa: BLE001 - a dead endpoint is a finding
                if _is_blocked(exc, name):
                    raise Blocked(f"{name}: refused by the edge") from exc
                failures.append(
                    f"{name}: probe crashed with `{exc!r}`\n"
                    f"```\n{traceback.format_exc()}```"
                )
    finally:
        client.close()
    return failures


def run_checks(
    attempts: int = MAX_ATTEMPTS, wait: float = RETRY_WAIT_SECONDS
) -> List[str]:
    """Probe the live API, retrying a blocked run on a fresh session.

    :raises Blocked: if every attempt was blocked by the edge.
    """
    for attempt in range(1, attempts + 1):
        try:
            return _run_once()
        except Blocked as exc:
            print(f"Attempt {attempt}/{attempts} blocked ({exc}).", file=sys.stderr)
            if attempt == attempts:
                raise
            time.sleep(wait * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def write_report(path: str, failures: List[str]) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [
        "## Live API health check failed",
        "",
        f"Probed the live Wizz Air API on {today} (UTC); "
        f"{len(failures)} check(s) no longer hold:",
        "",
        *(f"- {f}" for f in failures),
        "",
        "If the upstream shape changed, update `flywizz/wire.py` and "
        "`docs/internal-api-spec.md` to match.",
        "",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        default=None,
        help="write a markdown failure report to this path (only on failure)",
    )
    args = parser.parse_args()

    try:
        failures = run_checks()
    except Blocked as exc:
        # Not drift: the edge refused this IP outright, so there is no shape
        # to compare. Report it as its own outcome and leave wire.py alone.
        print(
            f"Live API probe blocked after {MAX_ATTEMPTS} attempts ({exc}) — "
            "no shape checked.",
            file=sys.stderr,
        )
        return BLOCKED_EXIT

    if not failures:
        print("Live API health check passed.")
        return 0

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if args.report:
        write_report(args.report, failures)
    return 1


if __name__ == "__main__":
    sys.exit(main())
