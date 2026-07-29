"""Testes de integração contra a Azure Retail Prices API REAL (pública).

Marcados com @pytest.mark.integration e puláveis offline (skip sem rede).
Rodar só integração:   uv run pytest -m integration
Pular integração:      uv run pytest -m "not integration"
"""

import pytest

from azure_estimator_mcp.azure.meters import PriceResolutionError
from azure_estimator_mcp.azure.pricing import monthly_cost, resolve_price

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _skip_offline(has_network):
    if not has_network:
        pytest.skip("sem rede: pulando testes de integração")


async def test_vm_real_price():
    result = await resolve_price(
        "vm", {"armSkuName": "Standard_D2s_v3", "region": "eastus"}
    )
    assert result.unit_price > 0
    assert "Hour" in result.unit_of_measure
    # Custo mensal plausível > 0.
    assert monthly_cost(result, {}) > 0


async def test_storage_real_price():
    try:
        result = await resolve_price(
            "storage", {"region": "eastus", "redundancy": "LRS", "tier": "Hot"}
        )
    except PriceResolutionError as exc:
        pytest.fail(f"Storage não resolveu para um único meter: {exc}")
    assert result.unit_price > 0
    assert "GB" in result.unit_of_measure


async def test_sql_real_price():
    try:
        result = await resolve_price(
            "sql",
            {"region": "eastus", "tier": "General Purpose",
             "compute": "Provisioned", "vCores": 2},
        )
    except PriceResolutionError as exc:
        pytest.fail(f"SQL não resolveu para um único meter: {exc}")
    assert result.unit_price > 0
