# AGENTS.md

Notes for AI agents working in this repo. The file follows the
[agents.md](https://agents.md) convention so Claude Code, Cursor, Aider, and
similar tools pick it up automatically.

## What Flywizz is

An unofficial Python SDK for Wizz Air's undocumented public flight API.

There is no API contract from Wizz Air. Behaviour is reverse-engineered from
poking the live endpoints and reading the SPA bundle; the source of truth for
endpoint quirks is `docs/internal-api-spec.md`. Update that doc whenever an
endpoint's behaviour, params, or response shape changes.

Sibling project: Flyan, the same idea for Ryanair. The layout is deliberately
parallel, but the APIs are not: do not copy Ryanair assumptions across (see
"Where Wizz differs from Ryanair" below).

## Layout

```
flywizz/
  wizzair.py        # Sync client (WizzAir). Domain-friendly methods that
                    # build paths, delegate to transport, and call parsers.
  async_client.py   # Async mirror (AsyncWizzAir). Keep in lockstep with
                    # wizzair.py: every public method on one should exist on
                    # the other with the same signature.
  transport.py      # HTTP seam. Owns httpx, retries, UA rotation, the two-step
                    # session handshake, the rotating verification token, and
                    # API-version discovery. Adapters: WizzairTransport,
                    # AsyncWizzairTransport, CachingTransport.
  wire.py           # Translation between public models and Wizz wire format.
                    # Field-name quirks (flightList, dayInterval, bare dates vs
                    # datetimes) live HERE and nowhere else.
  misc.py           # Public Pydantic models (Price, Station, Network,
                    # TimetableEntry, FareChartEntry, ...). No wire knowledge.
  mcp_server.py     # MCP server exposing curated tools to agents.

docs/
  internal-api-spec.md  # Living notes on the undocumented Wizz Air API.
                        # READ THIS before adding or modifying an endpoint.
```

## Architectural seams

Three seams, and the split is load-bearing. Do not collapse them.

1. **transport** knows HTTP, cookies, tokens, retries, versioning. Returns
   parsed JSON. Knows nothing about flights.
2. **wire** knows Wizz Air's field names and quirks. Translates between `misc`
   models and wire dicts.
3. **coordinator** (`wizzair.py` / `async_client.py`) exposes domain-friendly
   methods. Each is a few lines: build path + body, call transport, call parser.

If you find yourself putting wire-format strings in `wizzair.py` or HTTP
concerns in `wire.py`, you're working against the structure.

## The two things that will bite you

### 1. The verification token rotates

Every request needs `X-RequestVerificationToken` matching the current
`RequestVerificationToken` cookie. The server may reissue the cookie on any
response, so the transport re-reads the jar per request. Never cache the value.

A stale or missing token produces `400 {"handlerError":"InvalidProtocol"}` on
**every** endpoint. If everything suddenly says `InvalidProtocol`, it is the
token, not your payload.

### 2. Four endpoints sit behind Kasada

`search/search`, `booking/seatmap`, `booking/ancillaries` and
`booking/passengers` answer `429` with an empty body from any non-browser
client. Flywizz raises `BotGateError` and does not attempt to solve the
challenge. `WizzairTransport(kasada_headers=...)` lets a caller who already has
a live browser session hand the `x-kpsdk-*` headers in.

Do not add a retry for 429. It is a gate, not backpressure.

## Where Wizz differs from Ryanair

| | Ryanair | Wizz Air |
|---|---|---|
| Country codes | lowercase iso2 | **uppercase** ISO2 |
| Auth | anonymous | session handshake + rotating CSRF token |
| Currency | `currency` query param | fixed to the departure station's currency |
| Cheap-fare search | `oneWayFares` with "anywhere" | no anywhere search; fan out per route |
| Priced surface | one endpoint | `timetableV2` (open) vs `search/search` (gated) |
| Pagination | `nextPage` | none |

## Conventions

### Sync and async parity
Every public method on `WizzAir` has an equivalent on `AsyncWizzAir`. Add to
both, or neither.

### Error contract
Public methods return `[]` only when the API answered with no matching results.
Anything else raises: `ValidationError` when the API returned its own
`validationCodes`, `BotGateError` on 429, `WizzairException` otherwise.

### Dates are `datetime`, not `date`, in the public API
The wire layer reduces to the right string form. `timetableV2` and `farechart`
take bare ISO dates; `search/search` takes ISO datetimes at midnight. That
inconsistency is the API's, and it is handled in `wire.py`.

### Pydantic models, not dataclasses
Validation lives on the model. New public structures should be `BaseModel`
subclasses in `misc.py`.

### No comments that explain *what*
Self-documenting code, plus one-line *why* comments only when the constraint is
non-obvious (a hidden API quirk, a workaround for a specific bug).

### Avoid em dashes in user-facing prose
README, release notes, and this file use commas, periods, or colons instead.

## When you add an endpoint

1. Check `docs/internal-api-spec.md` first.
2. Probe the live shape before writing the parser. An empty `{}` POST usually
   returns `validationCodes` naming the required fields, which is the fastest
   way to discover a schema. `404` vs `405` tells you whether a route exists.
3. Add the wire translation to `wire.py`.
4. Add the public method to `wizzair.py` AND `async_client.py`.
5. If the response is a new domain object, add the model to `misc.py` and export
   it from `flywizz/__init__.py`.
6. Update `docs/internal-api-spec.md`.
7. Consider whether the new endpoint deserves an MCP tool in
   `flywizz/mcp_server.py` (rule of thumb: yes if it answers a
   natural-language question, no if it's a low-level building block, and
   never if it's bot-gated - an agent tool that always fails is worse than
   no tool). If you add or remove one, update the tool count asserted in
   `tests/test_mcp_server.py` and `.github/workflows/ci.yml`.

## Testing

Three layers, and they answer different questions.

```bash
.venv/bin/pip install -e ".[mcp,dev]"

.venv/bin/python -m pytest                              # offline, fixtures
FLYWIZZ_LIVE=1 .venv/bin/python -m pytest tests/functional -v -rs
.venv/bin/python scripts/api_health_check.py            # raw shapes
```

**`tests/` (offline)** covers the pure layers -- wire serialization, parsers,
transport plumbing -- against fixtures. Fast and deterministic, and blind to
the API changing underneath us. Default `pytest` runs only this;
`tests/functional` skips itself without `FLYWIZZ_LIVE`.

**`tests/functional/` (live)** answers "is the SDK stable against the API
today", through `WizzAir` and `AsyncWizzAir` rather than raw JSON. It asserts
*invariants*, never values: a fare is never zero, prices come in the departure
station's currency, a day never appears twice, sync and async agree, the
derived helpers return real minima. Add a test here whenever you find a bug
that a fixture could not have caught -- both bugs found so far (a placeholder
price read as `0`, and `cheapest_weekend` exceeding the API's window cap at
its own default) were invisible offline.

It skips rather than fails on a 5xx, an edge block, or an empty window, so red
means the SDK and the API genuinely disagree. Read the `-rs` skip list: a test
that skipped guarded nothing.

**`scripts/api_health_check.py`** probes raw response shapes at the transport
level and runs daily. Exits 0 on success, 1 on shape drift (writing a markdown
report with `--report`), and 2 when the edge refused the runner outright -- the
workflow turns a 2 into a warning rather than an issue, because a blocked probe
says nothing about the shape.

Wrap the transport in `CachingTransport` (or `AsyncCachingTransport`) when
iterating on network-metadata work so you don't refetch `asset/map` every run.

### Measure before you optimise

`FlightDatesMultiArrival` looked like an obvious way to cut `explore_with_fares`
down: rule out the routes that don't operate, then price only the survivors.
Measured, it isn't. The prefilter costs `N/5` requests and saves one per dead
route, so it only pays off above 20% dead routes; BUD, KTW and EIN came in at
18%, 22% and 11%. The method stays as a public endpoint wrapper, where it is a
genuine 5x saving for schedule-only questions, and `explore_with_fares` was left
alone. Take the measurement before wiring an endpoint into a helper.

## Release flow

Releases are automated:

1. Land conventional commits to `master` (`feat:`, `fix:`, `docs:`, etc.).
2. The `release-please` workflow opens a release PR.
3. Merge the release PR. The tag is pushed from there.
4. `release.yml` builds and publishes to PyPI on the tag, then pushes the
   server entry to the MCP Registry.

`CHANGELOG.md` is gitignored. The canonical changelog is the GitHub Releases
page, which release-please populates.

Version is tracked in three places that must stay in sync: `pyproject.toml`,
`.release-please-manifest.json`, and `server.json` (both the top-level
`version` and `packages[0].version`, though `release.yml` rewrites those from
the tag at publish time).

## Commit messages

- Use Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
  `test:`).
- **Do not add `Co-Authored-By:` trailers for AI assistants.** No "Generated
  with" footers either.
- Keep the subject under 72 chars, imperative mood.

## What not to do

- Don't import `httpx` outside `transport.py`.
- Don't cache the verification token.
- Don't retry a 429.
- Don't add fallbacks for "what if Wizz changes the field name". We want to fail
  loudly so we update the parser.
- Don't add retry logic outside `transport.py`.
