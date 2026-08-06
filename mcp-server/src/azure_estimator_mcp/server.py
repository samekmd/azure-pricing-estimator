"""Servidor MCP do estimador de preços do Azure.

As tools de descoberta (search_azure_services, get_service_config_schema) e de
preço (resolve_price, estimate_monthly_cost) estão ligadas à implementação real
em azure/catalog.py e azure/pricing.py — aqui elas são só adaptadores: traduzem
argumentos, chamam a função e serializam. Os outros 4 tools seguem como stubs —
dependem de azure/calculator_client.py, que ainda é Fase 2.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .azure.catalog import DEFAULT_REGION, DEFAULT_SAMPLE
from .azure.catalog import config_schema as _config_schema
from .azure.catalog import search_services as _search_services
from .azure.pricing import monthly_cost as _monthly_cost
from .azure.pricing import resolve_price as _resolve_price

mcp = MCPServer("azure-pricing-estimator")


@mcp.tool()
async def search_azure_services(
    query: str, currency: str = "USD"
) -> list[dict[str, Any]]:
    """Encontra serviços Azure a partir de um termo em linguagem natural.

    Devolve, para cada candidato, o `service_name` EXATO da Retail Prices API, a
    `key` a ser usada em resolve_price/get_service_config_schema, um rótulo e a
    família do serviço. Termo sem correspondência devolve lista vazia.
    """
    matches = await _search_services(query, currency=currency)
    return [m.model_dump() for m in matches]


@mcp.tool()
async def get_service_config_schema(
    service: str,
    region: str = DEFAULT_REGION,
    currency: str = "USD",
    sample_size: int = DEFAULT_SAMPLE,
) -> dict[str, Any]:
    """Campos de config que um serviço espera, com os valores válidos da API.

    `service` aceita a key ('vm'), o serviceName ('Virtual Machines') ou um
    alias. Enumerações grandes vêm recortadas: amostra + total + a consulta da
    API que produz a lista completa. O `example_config` devolvido já é uma config
    aceita por resolve_price.
    """
    schema = await _config_schema(
        service, region=region, currency=currency, sample_size=sample_size
    )
    return schema.model_dump()


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
