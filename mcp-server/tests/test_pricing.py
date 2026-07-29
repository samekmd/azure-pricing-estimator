"""Testes de monthly_cost e resolve_price (mockado, sem rede)."""

import httpx
import pytest
import respx

from azure_estimator_mcp.azure.pricing import monthly_cost, resolve_price
from azure_estimator_mcp.azure.retail_client import BASE_URL, RetailPricesClient
from azure_estimator_mcp.models import PriceResult


def _price(uom, unit_price=1.0):
    return PriceResult(
        unit_price=unit_price, currency="USD", unit_of_measure=uom,
        meter_id="m", meter_name="n", sku_name="s", region="eastus",
        price_type="Consumption",
    )


def test_monthly_cost_hour_default_730():
    assert monthly_cost(_price("1 Hour", 0.1), {}) == pytest.approx(73.0)


def test_monthly_cost_hour_custom():
    assert monthly_cost(_price("1 Hour", 0.1), {"hours": 100}) == pytest.approx(10.0)


def test_monthly_cost_gb_month():
    assert monthly_cost(_price("1 GB/Month", 0.02), {"gb": 500}) == pytest.approx(10.0)


def test_monthly_cost_with_factor():
    # "100 GB/Month" -> preço é por 100 GB.
    assert monthly_cost(_price("100 GB/Month", 5.0), {"gb": 300}) == pytest.approx(15.0)


def test_monthly_cost_per_month_fixed():
    assert monthly_cost(_price("1/Month", 4.2), {}) == pytest.approx(4.2)


def test_monthly_cost_unknown_unit_raises():
    with pytest.raises(ValueError):
        monthly_cost(_price("1 Transaction"), {})


def test_monthly_cost_gb_missing_usage_raises():
    with pytest.raises(ValueError):
        monthly_cost(_price("1 GB/Month"), {})


@respx.mock
async def test_resolve_price_builds_price_result():
    item = {
        "retailPrice": 0.096, "currencyCode": "USD", "unitOfMeasure": "1 Hour",
        "serviceName": "Virtual Machines", "productName": "Virtual Machines Dv3 Series",
        "skuName": "D2s v3", "armSkuName": "Standard_D2s_v3",
        "meterId": "abc-123", "meterName": "D2s v3", "armRegionName": "eastus",
        "type": "Consumption", "isPrimaryMeterRegion": True,
    }
    respx.get(BASE_URL).mock(
        return_value=httpx.Response(200, json={"Items": [item], "NextPageLink": None})
    )
    async with RetailPricesClient() as client:
        result = await resolve_price(
            "vm", {"armSkuName": "Standard_D2s_v3", "region": "East US"}, client=client
        )
    assert isinstance(result, PriceResult)
    assert result.unit_price == 0.096
    assert result.meter_id == "abc-123"
    assert result.region == "eastus"  # normalizado
    assert result.unit_of_measure == "1 Hour"
