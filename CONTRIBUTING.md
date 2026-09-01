# Contributing to Flywizz

Thanks for taking the time to contribute. Flywizz is a small project and
contributions of every size are welcome: bug reports, endpoint changes
you've spotted in the wild, doc fixes, and code.

This file is the public front door. If you're going to make non-trivial
changes to the code, read [`AGENTS.md`](./AGENTS.md) as well — it
documents the architectural seams and conventions in more depth, and
[`docs/internal-api-spec.md`](./docs/internal-api-spec.md) is the source
of truth for what the upstream API actually does.

## Code of conduct

By participating you agree to keep interactions respectful and on-topic.
We follow the spirit of the [Contributor Covenant][cc]. Harassment,
personal attacks, and bad-faith engagement are not welcome.

[cc]: https://www.contributor-covenant.org/version/2/1/code_of_conduct/

## Ways to contribute

- **Report a bug.** Open a [bug report][bug] with a minimal reproduction.
  Include the Flywizz version, Python version, and the full traceback.
- **Report an endpoint change.** Wizz Air's API is undocumented and
  changes without warning. If a response shape or field name has
  drifted, use the [endpoint change template][endpoint] — it asks for
  the request, the response you got, and what you expected.
- **Propose a feature.** Open a [feature request][feature] and describe
  the use case before you write code. Small features can skip this; for
  anything that adds a public method or changes a model, the discussion
  saves both of us a redesign.
- **Send a pull request.** See below.

[bug]: https://github.com/victorlane/flywizz/issues/new?template=bug_report.yml
[endpoint]: https://github.com/victorlane/flywizz/issues/new?template=endpoint_change.yml
[feature]: https://github.com/victorlane/flywizz/issues/new?template=feature_request.yml

## Development setup

Flywizz uses [uv][uv] for environment management, but a plain `venv` works
fine too.

```sh
git clone https://github.com/victorlane/flywizz
cd flywizz
uv venv
source .venv/bin/activate
uv pip install -e '.[dev,mcp]'
pre-commit install
```

Supported Python versions: 3.10, 3.11, 3.12, 3.13.

[uv]: https://docs.astral.sh/uv/

## Running checks

Before opening a PR, run:

```sh
ruff check .
ruff format --check .
pytest
```

`pre-commit` runs the same lint hooks on commit. CI runs the full suite
against every supported Python version.

`pytest` runs entirely against fixtures: fast, offline, deterministic.

### Live tests

Two things talk to the real API, and both are opt-in:

```sh
# Raw response shapes, endpoint by endpoint. Also runs daily in CI.
python scripts/api_health_check.py

# The SDK's own behaviour against the live API, via WizzAir/AsyncWizzAir.
FLYWIZZ_LIVE=1 pytest tests/functional -v -rs
```

The functional suite asserts *invariants*, not values — prices move every
hour, "a fare is never zero" does not. It skips rather than fails when Wizz
Air is down, when the edge blocks the runner, or when a window is genuinely
empty, so a red run means the SDK and the API actually disagree.

Run both if your change touches `transport.py`, `wire.py`, `misc.py`, or
either client, and say what you ran in the PR description. `-rs` prints the
skip reasons; check them, because a suite that skipped the thing you changed
has not tested it.

## Pull request flow

1. Branch from `master`.
2. Make focused commits. One PR, one concern.
3. Use [Conventional Commits][cc-commits] for commit messages and the PR
   title — `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`. The
   `release-please` workflow uses these to generate the changelog and
   bump the version, so getting the prefix right matters.
4. Update `AGENTS.md` or `docs/internal-api-spec.md` if you're changing
   architectural seams or endpoint behaviour.
5. Open the PR against `master`. Fill in the PR template — the test plan
   section is not optional.
6. CI must be green. A maintainer will review and either merge, ask for
   changes, or explain why the change isn't a fit.

[cc-commits]: https://www.conventionalcommits.org/

## Conventions worth knowing up front

These trip up new contributors. The deeper rationale is in `AGENTS.md`.

- **The verification token rotates.** Every request carries
  `X-RequestVerificationToken` matching the current cookie, re-read from
  the jar each time. Caching it produces
  `400 {"handlerError":"InvalidProtocol"}` on request two, which looks
  like a payload bug and isn't.
- **Sync and async stay in lockstep.** Every public method on `WizzAir`
  has a matching one on `AsyncWizzAir`. Add to both.
- **Country codes are uppercase ISO2.** Wizz Air is the opposite of
  Ryanair here; don't copy that habit across from Flyan.
- **Wire-format knowledge lives in `wire.py`.** Field-name quirks
  (`flightList`, `dayInterval`, bare dates on timetable vs datetimes on
  availability) don't leak into the coordinator layer.
- **`httpx` imports are confined to `transport.py`.** Everything else
  takes a `Transport` / `AsyncTransport`.
- **Never retry a 429.** It's the Kasada bot gate, not backpressure.
- **Public methods return `[]` for empty results and raise
  `WizzairException` for everything else.** No silent failures.

## Releasing

Releases are automated by [`release-please`][rp]. Maintainers merge the
generated release PR; the tag, PyPI publish, and MCP Registry publish
happen from there. You don't need to bump versions in your PR.

[rp]: https://github.com/googleapis/release-please

## Security issues

Don't open a public issue for a security report. See
[`SECURITY.md`](./SECURITY.md) for how to report privately.

## Questions

If you're not sure whether something is in scope, open a discussion or a
draft PR and ask. Better to check first than to build something we'd
have to turn away.
