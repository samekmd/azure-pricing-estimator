"""Testes de paginação do RetailPricesClient (mockados, sem rede)."""

import httpx
import pytest
import respx

from azure_estimator_mcp.azure.retail_client import (
    BASE_URL,
    PAGE_SIZE,
    RetailPricesClient,
    build_filter,
    normalize_region,
)


def _page(items, next_link=None):
    body = {"Items": items, "Count": len(items), "NextPageLink": next_link}
    return httpx.Response(200, json=body)


def _items(n, start=0):
    return [{"meterId": f"m{start + i}", "retailPrice": 0.1} for i in range(n)]


@respx.mock
async def test_single_page():
    respx.get(BASE_URL).mock(return_value=_page(_items(3)))
    async with RetailPricesClient() as c:
        items = await c.query_prices({"serviceName": "Virtual Machines"})
    assert len(items) == 3


@respx.mock
async def test_follows_next_page_link():
    page2_url = f"{BASE_URL}?api-version=x&$skip=1000"
    route = respx.get(BASE_URL)
    # 1ª chamada (com params) -> página cheia + NextPageLink; 2ª (link) -> fim.
    route.side_effect = [
        _page(_items(PAGE_SIZE), next_link=page2_url),
        _page(_items(5, start=PAGE_SIZE)),
    ]
    async with RetailPricesClient() as c:
        items = await c.query_prices({"serviceName": "Storage"})
    assert len(items) == PAGE_SIZE + 5


@respx.mock
async def test_empty_next_link_workaround_uses_skip():
    """Bug conhecido: página cheia SEM NextPageLink -> cliente gera $skip sozinho."""
    calls = {"skips": []}

    def responder(request):
        skip = request.url.params.get("$skip")
        calls["skips"].append(skip)
        if skip is None:  # 1ª página: cheia, SEM next link
            return _page(_items(PAGE_SIZE), next_link=None)
        if skip == str(PAGE_SIZE):  # 2ª página: cheia, ainda sem link
            return _page(_items(PAGE_SIZE, start=PAGE_SIZE), next_link=None)
        return _page(_items(7, start=2 * PAGE_SIZE), next_link=None)  # 3ª: incompleta -> fim

    respx.get(BASE_URL).mock(side_effect=responder)
    async with RetailPricesClient() as c:
        items = await c.query_prices({"serviceName": "SQL Database"})

    assert len(items) == 2 * PAGE_SIZE + 7
    # Confirma que o contorno incrementou $skip corretamente.
    assert calls["skips"] == [None, str(PAGE_SIZE), str(2 * PAGE_SIZE)]


@respx.mock
async def test_retry_on_429_then_success():
    route = respx.get(BASE_URL)
    route.side_effect = [
        httpx.Response(429, json={}),
        _page(_items(2)),
    ]
    async with RetailPricesClient(backoff_base=0) as c:
        items = await c.query_prices({"serviceName": "Storage"})
    assert len(items) == 2


def test_build_filter_and_region_normalization():
    f = build_filter(
        {"serviceName": "Virtual Machines", "armRegionName": "East US"}
    )
    assert "serviceName eq 'Virtual Machines'" in f
    assert "armRegionName eq 'eastus'" in f  # normalizado
    assert " and " in f
    assert normalize_region("West Europe") == "westeurope"
    # Escape de aspas simples (OData: dobra a aspa).
    assert "''" in build_filter({"productName": "O'Brien"})
