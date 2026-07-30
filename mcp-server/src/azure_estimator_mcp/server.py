"""Servidor MCP do estimador de preços do Azure.

resolve_price e estimate_monthly_cost estão ligados à implementação real em
azure/pricing.py. Os outros 4 tools seguem como stubs — dependem de
azure/calculator_client.py, que ainda é Fase 2.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .azure.pricing import monthly_cost as _monthly_cost
from .azure.pricing import resolve_price as _resolve_price

mcp = MCPServer("azure-pricing-estimator")


@mcp.tool()
async def resolve_price(
    service: str, config: dict[str, Any], currency: str = "USD"
) -> dict[str, Any]:
    """Resolve o preço unitário de um serviço Azure a partir da sua config."""
    price = await _resolve_price(service, config, currency=currency)
    return price.model_dump()


@mcp.tool()
async def estimate_monthly_cost(
    service: str,
    config: dict[str, Any],
    usage: dict[str, Any],
    currency: str = "USD",
) -> float:
    """Estima o custo mensal de um serviço a partir de config + uso."""
    price = await _resolve_price(service, config, currency=currency)
    return _monthly_cost(price, usage)


@mcp.tool()
async def check_calculator_auth() -> bool:
    """Reporta se há uma sessão autenticada salva para a calculadora Azure.

    TODO: wire to azure.calculator_client.AzureCalculatorClient.is_authenticated.
    """
    raise NotImplementedError(
        "TODO: wire to AzureCalculatorClient.is_authenticated"
    )


@mcp.tool()
async def create_estimate() -> dict[str, Any]:
    """Cria uma nova estimativa na calculadora Azure.

    TODO: wire to azure.calculator_client.AzureCalculatorClient.create_estimate
    (Fase 2 — ainda stub em calculator_client.py).
    """
    raise NotImplementedError(
        "TODO: wire to AzureCalculatorClient.create_estimate"
    )


@mcp.tool()
async def add_line_item(service: str, config: dict[str, Any]) -> dict[str, Any]:
    """Adiciona um serviço à estimativa atual na calculadora Azure.

    TODO: wire to azure.calculator_client.AzureCalculatorClient.add_line_item
    (Fase 2 — ainda stub em calculator_client.py).
    """
    raise NotImplementedError(
        "TODO: wire to AzureCalculatorClient.add_line_item"
    )


@mcp.tool()
async def export_estimate() -> str:
    """Compartilha a estimativa atual e devolve o link gerado.

    TODO: wire to azure.calculator_client.AzureCalculatorClient.export_estimate
    (Fase 2 — ainda stub em calculator_client.py).
    """
    raise NotImplementedError(
        "TODO: wire to AzureCalculatorClient.export_estimate"
    )


if __name__ == "__main__":
    mcp.run()
