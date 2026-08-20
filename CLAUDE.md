# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MCP server that estimates Azure costs and (eventually) generates Azure Pricing
Calculator links. Code and comments in this repo are written in Portuguese
(pt-BR) — match that convention when editing existing files.

## Commands

Package management is via `uv`; the package source lives under `mcp-server/src`.

```bash
# install deps + Playwright's Chromium binary (wraps `uv sync` +
# `uv run playwright install chromium`; see Makefile)
make install

# run the full test suite (respx-mocked; integration tests skip without network)
uv run pytest
make test

# run a single test file / test
uv run pytest mcp-server/tests/test_meters.py
uv run pytest mcp-server/tests/test_meters.py::test_name -v

# run only integration tests (hit the real Retail Prices API)
uv run pytest -m integration
make test-integration

# one-time interactive login for the Playwright-driven calculator client
uv run python mcp-server/scripts/bootstrap_login.py
make bootstrap-login
```

`pyproject.toml` sets `testpaths = ["mcp-server/tests"]` and
`asyncio_mode = "auto"`, so async tests need no `@pytest.mark.asyncio` marker.

## Architecture

Two independent, deliberately decoupled subsystems live under
`mcp-server/src/azure_estimator_mcp/azure/`:

1. **Pricing lookup (`retail_client.py`, `meters.py`, `pricing.py`,
   `catalog.py`)** — pure HTTP (`httpx`), no browser, queries the public,
   unauthenticated Azure Retail Prices API
   (`https://prices.azure.com/api/retail/prices`).
   - `retail_client.py`: `RetailPricesClient` handles pagination and
     429/5xx retry with exponential backoff. Pagination is driven **only** by
     `$skip`, always derived from `len(items)` accumulated so far, stopping on
     a page shorter than `PAGE_SIZE=1000`; `NextPageLink` is never used as
     flow control (it comes back empty intermittently, and mixing the two
     modes desynchronized the counter and re-fetched pages). `top=N` caps a
     query for cheap discovery probes. `normalize_region()` slugifies
     commercial regions (`"East US"` → `"eastus"`), but the API's `$filter` is
     **case-sensitive** and non-commercial pseudo-regions come back with
     significant capitalization and spaces (`"Global"`, `"US Gov"`,
     `"Zone 1"`, `"Intercontinental"`, continent names). Slugifying those
     matched zero items and returned an empty list with no error, silently
     hiding every service without commercial-region meters (Load Balancer,
     bandwidth/egress, DNS, CDN…). `_SPECIAL_REGIONS` is an allowlist of those
     canonical spellings — every entry confirmed by probing the live API —
     consulted **before** the lower+replace slugify. Add a new pseudo-region
     there only after probing that the exact spelling returns items; do not
     work around it with client-side filtering. Note real Gov/DoD regions are
     ordinary slugs (`usgovvirginia`, `usdodeast`) and are not exceptions.
   - `meters.py`: one resolver per service (`resolve_vm`, `resolve_storage`,
     `resolve_sql`, registered in `RESOLVERS`). Each resolver returns
     `(odata_filters, select_fn)`: filters narrow the API query server-side,
     `select_fn` applies additional disambiguation that can't be expressed as
     an OData filter (e.g. excluding Spot/Windows variants by substring).
     **Hard rule**: if a selector can't isolate exactly one candidate meter,
     it must raise `PriceResolutionError` with the candidates attached rather
     than guess. When adding a new service resolver, follow this pattern.
   - `pricing.py`: `resolve_price()` wires a resolver to `RetailPricesClient`
     and returns a typed `PriceResult` (see `models.py`). `monthly_cost()`
     projects unit price to a monthly cost based on `unit_of_measure` — add
     new unit mappings here rather than guessing at unrecognized units
     (unrecognized units raise `ValueError` by design). Units are matched
     lowercased and stripped, after splitting off the numeric prefix, so
     spelling variants (`"1/Day"`, `"1 /Day"`, `"1 Day"`) hit the same branch —
     keep new mappings in that normalized form. Daily units project with
     `DAYS_PER_MONTH = 30`, a deliberate convention (not 365/12): ×30 is what
     matches Azure's own calculator (verified: ACR Premium $1.6666/day →
     $50/month).
   - `catalog.py`: discovery layer behind the `search_azure_services` and
     `get_service_config_schema` tools. `_CATALOG` maps each `RESOLVERS` key to
     the exact API `serviceName`, pt-BR/en aliases and a cheap probe filter;
     valid values come from live API probes cached in-memory by TTL
     (`CACHE_TTL_SECONDS`, 6h — call `clear_cache()` in tests). It describes
     what the resolvers consume; it must not resolve prices or pick meters.
     When adding a resolver to `meters.py`, add its entry + field builder here.

2. **Calculator automation (`calculator_client.py`,
   `scripts/bootstrap_login.py`)** — Playwright-driven browser automation
   against the authenticated Azure Pricing Calculator UI. This exists because
   the calculator's save endpoint requires a real authenticated browser
   session (cookie + CSRF) and rejects programmatic OAuth tokens
   (`AADSTS65002` was confirmed when this was tried).
   - `scripts/bootstrap_login.py`: standalone, run once, **sync** Playwright
     API. Opens a headed Chromium, blocks on `input()` for manual login
     (including MFA), then saves `storage_state` to
     `.auth/storage_state.json`.
   - `calculator_client.py`: `AzureCalculatorClient`, an async context
     manager (must use **async** Playwright API since it's called from async
     MCP tools) that loads the saved `storage_state` to drive the UI already
     authenticated. There is no auto-refresh of the session by design — if
     `is_authenticated()` returns `False`, rerun `bootstrap_login.py`.
     `create_estimate`, `add_line_item`, `export_estimate` are still stubs
     (Phase 2 — UI selectors for building/sharing an estimate aren't mapped
     yet); they raise `NotImplementedError`.

These two subsystems must stay decoupled — pricing lookup must never import
Playwright, and the calculator client must never call the Retail Prices API
directly.

### Security

`.auth/storage_state.json` (gitignored) holds a real, live session — cookies
and CSRF token. Never log its contents, never commit it, and don't add
auto-refresh logic that would need to persist credentials anywhere else.