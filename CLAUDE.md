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

# check the calculator's DOM selectors against the live page (opens a real
# Chromium; needs no session and publishes nothing)
uv run pytest -m browser
make test-browser

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
     `resolve_sql`, `resolve_aks`, `resolve_synapse`, registered in
     `RESOLVERS`). Each resolver returns
     `(odata_filters, select_fn)`: filters narrow the API query server-side,
     `select_fn` applies additional disambiguation that can't be expressed as
     an OData filter (e.g. excluding Spot/Windows variants by substring).
     **Hard rule**: if a selector can't isolate exactly one candidate meter,
     it must raise `PriceResolutionError` with the candidates attached rather
     than guess. When adding a new service resolver, follow this pattern.
     `resolve_aks` prices **only** the managed control plane fee — cluster
     nodes are ordinary VMs, already covered by `resolve_vm`. It deliberately
     does **not** call `_primary_only`: probed live, the right meter
     (`Standard Uptime SLA`) comes back with `isPrimaryMeterRegion=False`
     while the 6x pricier `Standard Long Term Support` add-on comes back
     `True`, so filtering on the primary region first would silently return
     the wrong meter — the same trap already documented for on-demand VMs.
     `resolve_synapse` covers **only** the serverless SQL pool. Its
     `serviceName` ("Azure Synapse Analytics") spans mutually incompatible
     billing axes — Dedicated SQL Pool by DWU/hour, Spark Pool by vCore/hour,
     Pipelines by operation, Storage by GB/month, plus dozens of SSIS VMs —
     so `SYNAPSE_TIERS` is an allowlist mapping tier to an **exact**
     `productName`, and an unsupported tier raises before any HTTP call.
     Matching "Serverless" as a substring would catch the Serverless *Apache
     Spark* Pool, priced per hour: the wrong product yields a plausible
     number, not an error.
     Note a resolver existing does not imply the service can be added to the
     calculator: `aks` and `synapse` resolve prices but have no
     `config_translate.py` mapping yet, so `add_line_item` refuses them with
     a clear error.
   - `pricing.py`: `resolve_price()` wires a resolver to `RetailPricesClient`
     and returns a typed `PriceResult` (see `models.py`). `monthly_cost()`
     projects unit price to a monthly cost based on `unit_of_measure` — add
     new unit mappings here rather than guessing at unrecognized units
     (unrecognized units raise `ValueError` by design). `"1 TB"` maps to
     `usage['tbProcessed']` (alias `usage['tb']`) and has **no default**:
     unlike hours, where 730 is a defensible full month, there is no
     "standard" volume queried, so a default would invent the whole bill. Units are matched
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

2. **Calculator automation (`calculator_client.py`, `config_translate.py`,
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
     `create_estimate`, `add_line_item`, `edit_line_item` and
     `export_estimate` are implemented, and the six MCP calculator tools in
     `server.py` are wired to them.
     The full pattern→link cycle was verified end-to-end on 2026-08-20.
     The browser session is a **module-level singleton** in `server.py`, not
     one client per tool call: `add_line_item`/`export_estimate` drive the
     page `create_estimate` stored in `self._page`, so an `async with` per
     tool would lose the estimate between calls. An `asyncio.Lock`
     serializes access — there is a single shared page. The singleton is
     closed by the server's `lifespan` on shutdown, by the `close_calculator`
     tool, and by an idle watchdog — but the watchdog only fires when **no
     estimate is open** (`_estimate_open`, cleared after a successful
     export), because the estimate lives only in the page and closing would
     destroy it silently.
     `add_line_item` takes config in the **calculator UI's** vocabulary, so
     `config_translate.py` (below) is what MCP callers go through; an
     unmapped field raises `NotImplementedError` instead of being silently
     dropped (same no-guessing rule as `meters.py`). All three services are
     mapped, including their billing radio groups; what is still missing is
     the storage **account quantity** (the storage panel's `count` is
     capacity, not quantity).
     Every field is resolved **inside the item's container**
     (`div[id="<slug>-<GUID>-layout"]`, one per line item, DOM order =
     insertion order), never by `.last` — that is what makes a specific item
     addressable: `add_line_item` returns the container id as `item_id` and
     `edit_line_item(item_id, config)` reapplies fields to that item alone,
     deriving the service from the id's slug. Scoping is not cosmetic: with
     two VMs the document holds two elements with `id="size"`. The GUID in
     the container id is the same one embedded in the billing radio ids, so
     scoping by container scopes the radios for free.
     Some billing groups are **conditional**: `osBillingOption` only exists
     when `operatingSystem=Windows`, and SQL's `softwareBillingOption` has
     only `payg`/`ahb` (no savings plan). A missing group raises a clear
     `ValueError` rather than a 30s Playwright timeout.
     `export_estimate` checks the session itself before clicking Share (the
     menu item stays `enabled` when logged out — its "Log in to Share" is a
     label, not a `disabled`) and raises `CalculatorAuthError`; the MCP tool
     also checks up front.
   - `config_translate.py`: the bridge between the two vocabularies —
     `armSkuName: Standard_D2s_v3` → `size: "D2s v3"`, `windows: false` →
     `operatingSystem: "Linux"`, `hardware: Gen5` → `generation:
     "Standard-series (Gen 5)"`. Pure dict→dict: it imports neither
     Playwright nor httpx, so it does not break the decoupling below and is
     testable offline. Two rules matter when editing it. **First**, every UI
     label is matched by exact text (`select_option(label=...)`), so labels
     must come from probing the live DOM, never from guessing — a wrong
     label fails as a Playwright timeout, not a clear error. **Second**,
     always emit what the config determines; never rely on a UI default.
     Four calculator defaults contradict what the resolvers assume:
     `operatingSystem`=Windows (resolver assumes Linux), `vcoreTier`=
     Hyperscale (resolver assumes General Purpose), and for SQL
     `databaseBillingOption`=3-year-reserved plus `softwareBillingOption`=
     Azure Hybrid Benefit — the last two alone price ~53% under on-demand
     with nothing on screen saying so. Whatever the config determines is
     emitted explicitly even when it matches the default; what it does not
     determine is returned in `assumptions`/`unsupported` for the caller to
     show the user, never dropped silently.

These two subsystems must stay decoupled — pricing lookup must never import
Playwright, and the calculator client must never call the Retail Prices API
directly.

### Security

`.auth/storage_state.json` (gitignored) holds a real, live session — cookies
and CSRF token. Never log its contents, never commit it, and don't add
auto-refresh logic that would need to persist credentials anywhere else.