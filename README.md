# Flywizz SDK

[![PyPI version](https://img.shields.io/pypi/v/Flywizz.svg)](https://pypi.org/project/Flywizz/)
[![Python versions](https://img.shields.io/pypi/pyversions/Flywizz.svg)](https://pypi.org/project/Flywizz/)
[![CI](https://github.com/victorlane/flywizz/actions/workflows/ci.yml/badge.svg)](https://github.com/victorlane/flywizz/actions/workflows/ci.yml)
[![CodeQL](https://github.com/victorlane/flywizz/actions/workflows/codeql.yml/badge.svg)](https://github.com/victorlane/flywizz/actions/workflows/codeql.yml)
[![PyPI downloads](https://img.shields.io/pypi/dm/Flywizz.svg)](https://pypi.org/project/Flywizz/)
[![License](https://img.shields.io/github/license/victorlane/flywizz.svg)](https://github.com/victorlane/flywizz/blob/master/LICENSE)

An open-source unofficial API wrapper to get flight data from Wizz Air.

<!-- mcp-name: io.github.victorlane/flywizz-mcp -->

> [!TIP]
> **MCP server for AI agents included.** Plug Flywizz into Claude Desktop,
> Claude Code, or Cursor and search Wizz Air flights in natural language.
> Jump to the [MCP Quickstart](#use-with-claude-cursor-and-other-mcp-clients).

Sibling project: [Flyan](https://github.com/victorlane/Flyan), the same idea
for Ryanair. Same layout, same seams. The APIs are not the same, though — see
[Wizz Air vs Ryanair](#wizz-air-vs-ryanair).

## Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Two things to know first](#two-things-to-know-first)
- [API Reference](#api-reference)
- [Data Models](#data-models)
- [Examples](#examples)
- [Explore Mode](#explore-mode)
- [**Use with Claude, Cursor, and other MCP clients**](#use-with-claude-cursor-and-other-mcp-clients)
- [Wizz Air vs Ryanair](#wizz-air-vs-ryanair)
- [Caching](#caching)
- [Rate Limiting](#rate-limiting)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)

## Installation

```bash
pip install Flywizz
```

Or using uv:

```bash
uv add Flywizz
```

## Quick Start

```python
from datetime import datetime, timedelta
from flywizz import WizzAir, TimetableSearch

# Initialize the client
client = WizzAir()

# Set up search parameters
search = TimetableSearch(
    origin="BUD",       # Budapest
    destination="LTN",  # London Luton
    date_from=datetime.now() + timedelta(days=30),
    date_to=datetime.now() + timedelta(days=60),
)

# One call gets the schedule and the prices
for day in client.get_timetable(search):
    if day.price is None:
        continue
    print(f"{day.departure_date.date()}: {day.price.amount} {day.price.currency}")
    print(f"  departures: {', '.join(d.departure.strftime('%H:%M') for d in day.departures)}")
```

Each entry is one operating day: the cheapest fare that day, plus every
departure time, so a single call answers both "when does it fly" and "what
does it cost".

## Two things to know first

### Prices are in the departure station's currency

Wizz Air has no server-side currency override. `BUD -> LTN` quotes in HUF,
`LTN -> BUD` quotes in GBP, `WAW -> LTN` in PLN. Body fields, query params,
cookies and headers named `currency` are all ignored.

Read `Station.currency_code` from `get_network()` if you need to know which
currency you'll get before you search, and convert client-side.

### `search/search` is behind a bot gate

Four endpoints (`search/search`, `booking/seatmap`, `booking/ancillaries`,
`booking/passengers`) sit behind Kasada and answer `429` with an empty body
from any non-browser client. Flywizz raises `BotGateError` rather than trying
to solve the challenge.

Everything else, including the priced `timetableV2` and `farechart` surfaces,
is open. That is enough for price tracking, route exploration and calendar
search. If you need fare bundles and sell keys, drive a real browser session
and pass its headers in:

```python
from flywizz import WizzAir, WizzairTransport

client = WizzAir(WizzairTransport(kasada_headers={
    "x-kpsdk-ct": "...",
    "x-kpsdk-v": "...",
    "x-kpsdk-h": "...",
    "x-kpsdk-cd": "...",
}))
```

Full details of the gate, the session handshake, and the whole route table are
in [`docs/internal-api-spec.md`](docs/internal-api-spec.md).

## API Reference

### WizzAir Class

#### Constructor

```python
WizzAir(transport: Optional[Transport] = None)
```

Creates a new Wizz Air client instance.

**Parameters:**

- `transport` (Transport, optional): Inject a custom transport, e.g. a
  `CachingTransport` wrapping the default, a `WizzairTransport` with
  `kasada_headers`, or a fixture transport for tests. Defaults to a fresh
  `WizzairTransport`.

**Example:**

```python
# Defaults
client = WizzAir()

# With caching for the 650 KB network metadata
from flywizz import CachingTransport, WizzairTransport
client = WizzAir(CachingTransport(WizzairTransport(), ttl=3600))
```

#### Methods

| Method | Endpoint | What it gives you |
|---|---|---|
| `get_network(language="en-gb")` | `asset/map` | Every station, its coordinates, currency, and connections |
| `get_destinations(origin, direct_only=True)` | derived | Stations reachable from `origin` |
| `explore_by_country(origin)` | derived | Destinations grouped by country code |
| `validate_route(origin, destination)` | derived | Does Wizz Air fly this route direct |
| `get_flight_dates(origin, destination, date_from, date_to)` | `search/flightDates` | Operating days, no prices, very cheap |
| `get_timetable(params)` | `search/timetableV2` | Cheapest fare per day, plus every departure |
| `get_return_timetable(params)` | `search/timetableV2` | Outbound and inbound in one call |
| `get_fare_chart(params)` | `asset/farechart` | Price strip around a target date |
| `get_availability(params)` | `search/search` | Fare bundles and sell keys. **Bot-gated** |
| `cheapest_in_month(origin, destination, month)` | derived | Cheapest day in a calendar month |
| `cheapest_weekend(origin, destination, months_ahead=3)` | derived | Cheapest Fri-Sun or Fri-Mon return |
| `explore_with_fares(origin, date_from, date_to, limit=None)` | derived | Every destination with its cheapest fare |
| `get_flight_status(carrier_code, flight_number, date=None)` | `asset/flightinformation` | Live status for one flight number |
| `get_currencies()` | `asset/currencies` | Supported ISO 4217 codes |
| `get_countries()` | `asset/country` | Countries with EU / Schengen flags |
| `get_cultures()` | `asset/cultures` | Site languages and their currencies |
| `get_service_fees(currencies=None)` | `asset/serviceFees` | Published baggage, seat and change fees |
| `get_wdc_prices()` | `asset/wdcPrice` | Discount Club tiers and minimum discounts |

Every method exists on `AsyncWizzAir` with the same signature.

### TimetableSearch Class

Parameters for a timetable search.

```python
TimetableSearch(
    origin: str,
    destination: str,
    date_from: datetime,
    date_to: datetime,
    return_date_from: Optional[datetime] = None,
    return_date_to: Optional[datetime] = None,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    price_type: str = "regular",
)
```

**Parameters:**

- `origin` (str): IATA code of the departure station (e.g. `"BUD"`)
- `destination` (str): IATA code of the arrival station (e.g. `"LTN"`)
- `date_from` (datetime): Start of the outbound departure window
- `date_to` (datetime): End of the outbound departure window
- `return_date_from` / `return_date_to` (datetime, optional): Inbound window.
  Both are required by `get_return_timetable()`
- `adults` / `children` / `infants` (int): Passenger counts. At least one adult
- `price_type` (str): `"regular"` or `"wdc"` for Wizz Discount Club pricing

### FareChartSearch Class

Parameters for the price strip.

```python
FareChartSearch(
    origin: str,
    destination: str,
    date: datetime,
    day_interval: int = 3,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    price_type: str = "regular",
)
```

`day_interval` is the half-window around `date` and must be at least 3, so the
default returns seven days. Smaller values are rejected upstream with
`DayIntervalMustBeGreaterOrEqualTo3`.

### AvailabilitySearch Class

Parameters for the bot-gated availability call.

```python
AvailabilitySearch(
    origin: str,
    destination: str,
    departure_date: datetime,
    return_date: Optional[datetime] = None,
    wdc: bool = True,
    is_flight_change: bool = False,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
)
```

## Data Models

### Price

Represents a money amount as Wizz Air reports it.

**Attributes:**

- `amount` (float): The amount
- `currency` (str): ISO 4217 code, always the departure station's currency
- `exchanged_amount` (Optional[float]): The SPA's client-side conversion hook.
  Stays `None` for anonymous sessions
- `exchanged_currency` (Optional[str]): Currency of `exchanged_amount`

### TimetableEntry

One operating day for a route, with its cheapest fare.

**Attributes:**

- `departure_station` (str), `arrival_station` (str): IATA codes
- `departure_date` (datetime): The operating day
- `price` (Optional[Price]): Cheapest fare that day, `None` if sold out
- `original_price` (Optional[Price]): Pre-discount price
- `departures` (list[Departure]): Every departure that day
- `price_type` (Optional[str]): `"price"` when there was inventory
- `has_mac_flight` (bool): The route includes a metropolitan-area alternative
- `applied_coupon_code` (Optional[str])

### Departure

**Attributes:**

- `departure` (datetime): Departure time
- `is_cheapest_of_the_day` (bool): This is the departure `price` refers to

### FareChartEntry

One day of the price strip.

**Attributes:**

- `departure_station` (str), `arrival_station` (str): IATA codes
- `day` (datetime): The day
- `price` (Optional[Price]): Cheapest price that day
- `class_of_service` (Optional[str]): Booking class the quote came from
- `price_type` (Optional[str]), `has_mac_flight` (bool)

### Station

An airport in Wizz Air's live network. Returned by the explore methods.

**Attributes:**

- `iata` (str): IATA station code
- `name` (str): Station name
- `country_code` (str): **Uppercase** ISO2 country code (e.g. `"HU"`, `"GB"`)
- `country_name` (str): Country name
- `currency_code` (str): Local currency. Fares from here are priced in it
- `latitude` (float), `longitude` (float): Coordinates
- `mac` (Optional[str]): Metropolitan area code (e.g. `"LON"`)
- `aliases` (list[str]): Alternative names
- `categories` (list[int]): Marketing categories assigned by Wizz Air
- `rank` (Optional[int]), `is_fake_station` (bool)
- `connections` (list[Connection]): Everywhere this station flies

Helper: `destinations(direct_only=True)` returns just the IATA codes.

### Connection

**Attributes:**

- `iata` (str): Destination station code
- `is_direct` (bool): A direct Wizz Air flight. The flag you usually want
- `is_connected` (bool): A self-transfer connection rather than a direct flight
- `is_domestic` (bool), `is_new` (bool)
- `operation_start_date` (Optional[datetime]): When the route opens

### FlightStatus

A single operating flight from the flight-information endpoint.

**Attributes:**

- `flight_id` (int), `carrier_code` (str), `flight_number` (int)
- `departure_airport` (str), `arrival_airport` (str)
- `original_departure_airport` / `original_arrival_airport` (Optional[str]):
  Differ from the actual airports when the flight was diverted
- `operation_day` (Optional[datetime])
- `scheduled_departure` / `scheduled_arrival` (Optional[datetime])
- `op_suffix` (Optional[str])

### DestinationFare

Returned by `explore_with_fares()`. Pairs a reachable destination with its
cheapest sampled fare, if one came back from the price probe.

**Attributes:**

- `station` (Station): The destination
- `price` (Optional[Price]): Cheapest fare in the window, or `None` if the
  route is in the network but no priced inventory came back
- `departure_date` (Optional[datetime]): The day that fare was on

## Examples

### Cheapest day in a month

```python
from datetime import datetime
from flywizz import WizzAir

client = WizzAir()
cheapest = client.cheapest_in_month("BUD", "LTN", datetime(2026, 11, 1))

if cheapest:
    print(f"{cheapest.departure_date.date()}: "
          f"{cheapest.price.amount} {cheapest.price.currency}")
```

### Is it cheaper a day either side?

```python
from datetime import datetime
from flywizz import WizzAir, FareChartSearch

client = WizzAir()
strip = client.get_fare_chart(
    FareChartSearch(origin="BUD", destination="LTN",
                    date=datetime(2026, 11, 10), day_interval=3)
)

for day in strip:
    price = f"{day.price.amount:.0f} {day.price.currency}" if day.price else "-"
    print(f"{day.day.date()}  {price}")
```

### Cheapest weekend in the next three months

```python
from flywizz import WizzAir

client = WizzAir()
weekend = client.cheapest_weekend("BUD", "LTN", months_ahead=3)

if weekend:
    out, back = weekend
    total = out.price.amount + back.price.amount
    print(f"{out.departure_date.date()} -> {back.departure_date.date()}: "
          f"{total} {out.price.currency}")
```

### Discount Club pricing

```python
from datetime import datetime, timedelta
from flywizz import WizzAir, TimetableSearch

client = WizzAir()
wdc = client.get_timetable(
    TimetableSearch(
        origin="BUD", destination="LTN",
        date_from=datetime.now() + timedelta(days=30),
        date_to=datetime.now() + timedelta(days=45),
        price_type="wdc",
    )
)
```

### Live flight status

```python
from flywizz import WizzAir

client = WizzAir()
for leg in client.get_flight_status("W6", "6201"):
    print(f"{leg.operation_day.date()} {leg.departure_airport} -> {leg.arrival_airport}")
```

Carrier codes are the AOC prefix: `W6` (Hungary), `W4` (Malta), `W9` (UK).

### Error Handling

```python
from flywizz import BotGateError, ValidationError, WizzairException

try:
    entries = client.get_timetable(search)
    if not entries:
        print("No flights found for the given criteria")
except ValidationError as e:
    print(f"Wizz Air rejected the request: {e.codes}")
except BotGateError:
    print("This endpoint needs a browser session")
except WizzairException as e:
    print(f"Wizz Air API error: {e}")
```

`ValidationError.codes` carries Wizz Air's own validation codes, which name
the fields it objected to. An empty list means the API answered with nothing
matching; it never means a failure.

## Explore Mode

Explore Mode answers the question "where can I actually fly from here?". It
reads Wizz Air's live network metadata once and exposes the reachable
destinations from any station, optionally grouped or joined with the cheapest
fare in a date window.

All methods below are available on both `WizzAir` and `AsyncWizzAir`.

### List every destination

```python
for station in client.get_destinations("BUD"):
    print(f"{station.iata} {station.name} ({station.country_code})")
```

Pass `direct_only=False` to include self-transfer connections.

### Group destinations

```python
by_country = client.explore_by_country("BUD")

print(f"BUD flies to {len(by_country)} countries")
for country, stations in sorted(by_country.items()):
    codes = ", ".join(s.iata for s in stations)
    print(f"  {country}: {codes}")
```

Country codes are **uppercase** ISO2.

### Check a single route

```python
client.validate_route("BUD", "LTN")  # True
```

### Destinations with their cheapest fare

`explore_with_fares()` joins the network destinations with a timetable probe,
so each destination comes back with its cheapest `Price` (or `None` if no
inventory was returned for that route in the window).

Wizz Air has no "anywhere" search, so this is one call per destination. Use
`limit` while iterating and wrap the transport in `CachingTransport`.

```python
from datetime import datetime, timedelta

start = datetime.now() + timedelta(days=30)
end = start + timedelta(days=14)

results = client.explore_with_fares("BUD", start, end, limit=20)

priced = [d for d in results if d.price is not None]
for d in sorted(priced, key=lambda d: d.price.amount)[:10]:
    print(f"{d.station.iata} {d.station.name}: "
          f"{d.price.amount} {d.price.currency}")
```

Prices across destinations are all in the **origin's** currency, so they are
directly comparable.

### Async usage

`AsyncWizzAir` mirrors every explore method, and `explore_with_fares()` fans
out concurrently:

```python
import asyncio
from datetime import datetime, timedelta
from flywizz import AsyncWizzAir

async def main():
    async with AsyncWizzAir() as client:
        results = await client.explore_with_fares(
            "BUD",
            datetime.now() + timedelta(days=30),
            datetime.now() + timedelta(days=45),
            limit=20,
            concurrency=5,
        )
        print(f"{sum(1 for r in results if r.price)} priced destinations")

asyncio.run(main())
```

If you call multiple explore methods in a row, wrap the transport in
`CachingTransport` so the network metadata is fetched once and reused.

## Use with Claude, Cursor, and other MCP clients

> [!IMPORTANT]
> Two commands and you're done:
>
> ```bash
> uv tool install "Flywizz[mcp]"
> claude mcp add flywizz flywizz-mcp
> ```
>
> Now your agent can search Wizz Air flights in natural language. No API
> keys, no accounts.

Flywizz ships an optional Model Context Protocol server so your agent can
search Wizz Air fares from natural-language prompts like *"what's the cheapest
day in November to fly Budapest to London"* or *"where can I fly from Budapest
in the first week of December"*.

### Quickstart

**1. Install Flywizz with the MCP extra:**

```bash
uv tool install "Flywizz[mcp]"
```

Or with pip:

```bash
pipx install "Flywizz[mcp]"
```

This installs a `flywizz-mcp` console script on your PATH.

**2. Add it to your agent:**

**Claude Code** (one-liner):

```bash
claude mcp add flywizz flywizz-mcp
```

**Claude Desktop**: open `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS (or `%APPDATA%\Claude\claude_desktop_config.json` on Windows) and add:

```json
{
  "mcpServers": {
    "flywizz": {
      "command": "flywizz-mcp"
    }
  }
}
```

Then restart Claude Desktop.

**Cursor**: Settings → MCP → Add new server, name `flywizz`, command
`flywizz-mcp`.

**3. Try it.** Ask your agent:

> "What's the cheapest day in November to fly from Budapest to London Luton?"

The agent should call `cheapest_day` with `origin="BUD"`,
`destination="LTN"`, `month="2026-11-01"`, then report the day and the price.

### Currency

There is no currency setting, because Wizz Air has none. Every tool returns
prices in the departure station's local currency and reports the code
alongside the amount. Tell your agent to quote the currency it gets back
rather than assuming euros.

### Exposed tools

The server exposes five curated tools so the agent can pick reliably:

- `find_fares` for "how much is BUD to LTN in November", with the full
  day-by-day breakdown and every departure time
- `cheapest_day` for "what's the cheapest day this month to fly X to Y"
- `price_around` for "is it cheaper a day either side of the 10th"
- `explore_destinations` for "what countries can I reach from X"
- `flight_status` for "when does W6 6201 operate"

No API keys, accounts, or rate-limit setup. The server reuses a single cached
`WizzAir` client across calls, so the network metadata is fetched once per
process.

The bot-gated `search/search` surface is deliberately not exposed: it cannot
work from a headless process, and an agent tool that always fails is worse
than no tool.

## Wizz Air vs Ryanair

If you're coming from [Flyan](https://github.com/victorlane/Flyan), these are
the differences that will bite you:

| | Ryanair (Flyan) | Wizz Air (Flywizz) |
|---|---|---|
| Country codes | lowercase iso2 | **uppercase** ISO2 |
| Auth | anonymous | session handshake + rotating CSRF token |
| Currency | `currency` query param | fixed to the departure station |
| Cheap-fare search | `oneWayFares` with "anywhere" | no anywhere search; fan out per route |
| Priced surface | one endpoint | `timetableV2` (open) vs `search/search` (gated) |
| Pagination | `nextPage` | none |
| Bot protection | WAF, occasional cold 403 | Kasada on four endpoints, permanent |

## Caching

`asset/map` is 650 KB and changes rarely. Wrap the transport when you call it
more than once:

```python
from flywizz import WizzAir, WizzairTransport, CachingTransport

client = WizzAir(CachingTransport(WizzairTransport(), ttl=3600))
```

`CachingTransport` never caches POSTs, so fares stay live. Call
`invalidate()` to drop the cache early.

## Rate Limiting

The SDK retries network errors and 5xx responses with exponential backoff, up
to 4 attempts. It deliberately does **not** retry a `429`: on this API that is
the Kasada bot gate rather than backpressure, and retrying just adds load
while still failing.

Wizz Air's API is anonymous, but it is not yours. Be a good citizen: cache the
network metadata, keep `explore_with_fares()` fan-out modest, and don't poll
fares faster than the prices actually change.

## Contributing

This is an open-source project. Contributions are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). Agent-facing notes on the architecture
live in [AGENTS.md](AGENTS.md), and everything known about the upstream API is
in [docs/internal-api-spec.md](docs/internal-api-spec.md).

## Disclaimer

This is an unofficial API wrapper and is not affiliated with Wizz Air. It
performs read-only requests against the public endpoints the airline's own
website uses. Use at your own risk and ensure you comply with Wizz Air's terms
of service.
