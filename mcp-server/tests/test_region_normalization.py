"""Testes da normalização de região (mockados, sem rede).

Trava o furo silencioso: pseudo-regiões não-comerciais ("Global", "US Gov",
"Zone 1"...) precisam chegar ao $filter com a capitalização exata da API. O
$filter é case-sensitive, então a grafia errada casa zero itens e retorna vazio
SEM erro — serviços como Load Balancer e bandwidth sumiam da resolução.
"""

import httpx
import pytest
import respx

from azure_estimator_mcp.azure.retail_client import (
    BASE_URL,
    RetailPricesClient,
    build_filter,
    normalize_region,
)


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        # Grafia canônica preservada...
        ("Global", "Global"),
        ("US Gov", "US Gov"),
        ("US Gov Zone 1", "US Gov Zone 1"),
        ("Zone 1", "Zone 1"),
        ("Intercontinental", "Intercontinental"),
        ("North America", "North America"),
        ("Middle East And Africa", "Middle East And Africa"),
        # ...e recuperada a partir de formas já achatadas/soltas, já que é essa
        # a forma que o código antigo produzia e que a API rejeita.
        ("global", "Global"),
        ("GLOBAL", "Global"),
        ("  Global  ", "Global"),
        ("usgov", "US Gov"),
        ("us gov", "US Gov"),
        ("zone1", "Zone 1"),
        ("usgovzone1", "US Gov Zone 1"),
    ],
)
def test_pseudo_regioes_preservam_grafia_da_api(entrada, esperado):
    assert normalize_region(entrada) == esperado


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("East US", "eastus"),
        ("east us", "eastus"),
        ("eastus", "eastus"),
        ("Brazil South", "brazilsouth"),
        ("West Europe", "westeurope"),
        # Fora do mapa mínimo: fallback genérico segue valendo.
        ("Germany West Central", "germanywestcentral"),
        # Regiões Gov/DoD REAIS vêm como slug comum na API e não são exceção.
        ("usgovvirginia", "usgovvirginia"),
        ("usdodeast", "usdodeast"),
        ("", ""),
    ],
)
def test_regioes_comerciais_nao_regridem(entrada, esperado):
    assert normalize_region(entrada) == esperado


def test_build_filter_emite_grafia_canonica():
    assert build_filter({"armRegionName": "Global"}) == "armRegionName eq 'Global'"
    assert build_filter({"armRegionName": "global"}) == "armRegionName eq 'Global'"
    assert build_filter({"armRegionName": "East US"}) == "armRegionName eq 'eastus'"


_LB_GLOBAL = {
    "retailPrice": 0.025,
    "currencyCode": "USD",
    "unitOfMeasure": "1 GB",
    "serviceName": "Load Balancer",
    "productName": "Load Balancer",
    "skuName": "Standard",
    "meterId": "lb-global-1",
    "meterName": "Global Data Processed",
    "armRegionName": "Global",
    "type": "Consumption",
}


@respx.mock
async def test_servico_global_e_encontrado_nao_volta_vazio():
    """Um meter com armRegionName='Global' É achado pela consulta.

    O mock imita a API: só responde com itens quando o $filter traz a grafia
    exata 'Global'. Com o lower+replace antigo o filtro dizia 'global' e a
    resposta vinha vazia, em silêncio — que é exatamente o que se trava aqui.
    """
    capturado = {}

    def responder(request):
        odata = request.url.params.get("$filter", "")
        capturado["filter"] = odata
        if "armRegionName eq 'Global'" in odata:
            return httpx.Response(200, json={"Items": [_LB_GLOBAL], "NextPageLink": None})
        return httpx.Response(200, json={"Items": [], "NextPageLink": None})

    respx.get(BASE_URL).mock(side_effect=responder)

    async with RetailPricesClient() as c:
        items = await c.query_prices(
            {"serviceName": "Load Balancer", "armRegionName": "Global"}
        )

    assert items, f"consulta voltou vazia; $filter emitido: {capturado.get('filter')!r}"
    assert items[0]["meterId"] == "lb-global-1"
    assert "armRegionName eq 'Global'" in capturado["filter"]


@respx.mock
async def test_regiao_comercial_continua_consultando_slug():
    """Contraprova: região comercial não passa a ser tratada como especial."""
    capturado = {}

    def responder(request):
        capturado["filter"] = request.url.params.get("$filter", "")
        return httpx.Response(200, json={"Items": [], "NextPageLink": None})

    respx.get(BASE_URL).mock(side_effect=responder)
    async with RetailPricesClient() as c:
        await c.query_prices({"serviceName": "Load Balancer", "armRegionName": "East US"})

    assert "armRegionName eq 'eastus'" in capturado["filter"]
