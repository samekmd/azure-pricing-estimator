"""Testes da tool de busca de serviços (catalog.search_services)."""

import httpx
import pytest
import respx

from azure_estimator_mcp.azure import catalog
from azure_estimator_mcp.azure.retail_client import BASE_URL

# Família por serviço, como a API devolve em serviceFamily.
_FAMILIAS = {
    "Virtual Machines": "Compute",
    "Storage": "Storage",
    "SQL Database": "Databases",
}


@pytest.fixture(autouse=True)
def _limpa_cache():
    """O cache do catálogo é de módulo — não pode vazar entre testes."""
    catalog.clear_cache()
    yield
    catalog.clear_cache()


def _responder(request):
    """Devolve 1 item do serviço citado no $filter; nada, se não reconhecer."""
    odata = request.url.params.get("$filter", "")
    for service, family in _FAMILIAS.items():
        if f"serviceName eq '{service}'" in odata:
            item = {
                "serviceName": service,
                "serviceFamily": family,
                "armRegionName": "eastus",
                "type": "Consumption",
            }
            return httpx.Response(
                200, json={"Items": [item], "Count": 1, "NextPageLink": None}
            )
    return httpx.Response(200, json={"Items": [], "Count": 0, "NextPageLink": None})


@pytest.fixture
def api_mockada():
    """Rota respx da Retail Prices API (não é autouse: integração usa a real)."""
    with respx.mock:
        yield respx.get(BASE_URL).mock(side_effect=_responder)


@pytest.mark.parametrize(
    "query, esperado, key",
    [
        ("banco de dados", "SQL Database", "sql"),
        ("máquina virtual", "Virtual Machines", "vm"),
        ("maquinas virtuais", "Virtual Machines", "vm"),
        ("VM", "Virtual Machines", "vm"),
        ("armazenamento", "Storage", "storage"),
        ("blob storage", "Storage", "storage"),
        ("Azure SQL", "SQL Database", "sql"),
    ],
)
async def test_query_conhecida_devolve_service_name_exato(
    api_mockada, query, esperado, key
):
    matches = await catalog.search_services(query)
    assert matches, f"nenhum candidato para {query!r}"
    assert matches[0].service_name == esperado
    assert matches[0].key == key
    assert matches[0].service_family == _FAMILIAS[esperado]


async def test_termo_sem_correspondencia_devolve_lista_vazia(api_mockada):
    assert await catalog.search_services("quantum blockchain") == []
    assert await catalog.search_services("") == []
    assert await catalog.search_services("   ") == []


async def test_service_name_devolvido_vem_da_api_nao_do_registro(api_mockada):
    """Se a API não confirma o serviceName, o candidato não é devolvido."""
    api_mockada.mock(
        return_value=httpx.Response(
            200, json={"Items": [], "Count": 0, "NextPageLink": None}
        )
    )
    assert await catalog.search_services("máquina virtual") == []


async def test_sondas_sao_cacheadas_entre_chamadas(api_mockada):
    await catalog.search_services("vm")
    chamadas = api_mockada.call_count
    assert chamadas > 0
    await catalog.search_services("vm")
    assert api_mockada.call_count == chamadas  # 2ª busca não bate na API


async def test_serializacao_amigavel_ao_agente(api_mockada):
    matches = await catalog.search_services("banco de dados")
    dumped = [m.model_dump() for m in matches]
    assert dumped[0]["service_name"] == "SQL Database"
    assert set(dumped[0]) == {
        "key",
        "service_name",
        "label",
        "service_family",
        "matched_on",
    }


@pytest.mark.integration
async def test_search_na_api_real(has_network):
    if not has_network:
        pytest.skip("sem rede: pulando teste de integração")
    matches = await catalog.search_services("banco de dados")
    assert [m.service_name for m in matches] == ["SQL Database"]
    assert matches[0].service_family  # a API preenche a família
