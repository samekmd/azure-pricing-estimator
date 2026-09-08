"""Testes da tool de schema de configuração (catalog.config_schema)."""

import httpx
import pytest
import respx

from azure_estimator_mcp.azure import catalog
from azure_estimator_mcp.azure.meters import RESOLVERS
from azure_estimator_mcp.azure.pricing import resolve_price
from azure_estimator_mcp.azure.retail_client import BASE_URL

# 60 SKUs de VM: mais que o sample_size default (25), para exercitar o recorte.
_VM_SKUS = ["Standard_D2s_v3", "Standard_B2ms"] + [
    f"Standard_F{i}s_v2" for i in range(58)
]
_VM_REGIOES = ["eastus", "westus2", "brazilsouth", "westeurope"]
_VM_TIPOS = ["Consumption", "Reservation", "DevTestConsumption"]

_SQL_PRODUTOS = [
    "SQL Database Single General Purpose - Compute Gen5",
    "SQL Database Single General Purpose - Serverless - Compute Gen5",
    "SQL Database Single Business Critical - Compute Gen5",
]
_SQL_VCORES = [2, 4, 8, 16]


@pytest.fixture(autouse=True)
def _limpa_cache():
    catalog.clear_cache()
    yield
    catalog.clear_cache()


def _pagina(items):
    return httpx.Response(
        200, json={"Items": items, "Count": len(items), "NextPageLink": None}
    )


def _responder(request):
    """Roteia cada sonda do catálogo pelo conteúdo do $filter."""
    odata = request.url.params.get("$filter", "")
    tem_regiao = "armRegionName eq" in odata
    ancora_vm = f"armSkuName eq '{catalog.VM_ANCHOR_SKU}'" in odata

    if "serviceName eq 'Virtual Machines'" in odata:
        if ancora_vm and not tem_regiao:  # sonda global de regiões
            return _pagina(
                [
                    {"armRegionName": r, "armSkuName": catalog.VM_ANCHOR_SKU}
                    for r in _VM_REGIOES
                ]
            )
        if ancora_vm:  # sonda de priceType (âncora + região)
            return _pagina(
                [
                    {"type": t, "armSkuName": catalog.VM_ANCHOR_SKU}
                    for t in _VM_TIPOS
                ]
            )
        return _pagina(  # sonda de SKUs da região
            [
                {"armSkuName": s, "serviceName": "Virtual Machines", "type": "Consumption"}
                for s in _VM_SKUS
            ]
        )

    if "serviceName eq 'SQL Database'" in odata:
        if "skuName eq '2 vCore'" in odata:  # âncora -> priceTypes
            return _pagina([{"type": t} for t in ("Consumption", "Reservation")])
        return _pagina(
            [
                {"productName": p, "skuName": f"{v} vCore", "type": "Consumption"}
                for p in _SQL_PRODUTOS
                for v in _SQL_VCORES
            ]
        )

    if "serviceName eq 'Storage'" in odata:
        if "skuName eq 'Hot LRS'" in odata:  # âncora -> priceTypes
            return _pagina([{"type": "Consumption"}])
        return _pagina(
            [
                {
                    "productName": "Blob Storage",
                    "skuName": f"{tier} {red}",
                    "meterName": f"{tier} {red} Data Stored",
                    "type": "Consumption",
                }
                for tier in ("Hot", "Cool", "Archive")
                for red in ("LRS", "GRS", "RA-GRS")
            ]
        )

    return _pagina([])


@pytest.fixture
def api_mockada():
    with respx.mock:
        yield respx.get(BASE_URL).mock(side_effect=_responder)


def _campos(schema):
    return {f.name: f for f in schema.fields}


async def test_vm_lista_campos_do_resolver(api_mockada):
    schema = await catalog.config_schema("vm", region="eastus")
    assert schema.key == "vm"
    assert schema.service_name == "Virtual Machines"
    assert set(_campos(schema)) == {"armSkuName", "region", "priceType", "windows"}


async def test_vm_traz_sku_real_conhecido_nos_valores_validos(api_mockada):
    schema = await catalog.config_schema("vm", region="eastus", sample_size=100)
    campos = _campos(schema)
    assert "Standard_D2s_v3" in campos["armSkuName"].values
    assert campos["armSkuName"].required is True
    assert "eastus" in campos["region"].values
    assert "Consumption" in campos["priceType"].values
    assert campos["priceType"].default == "Consumption"


async def test_enumeracao_grande_vem_recortada_com_contagem_e_origem(api_mockada):
    schema = await catalog.config_schema("vm", region="eastus")
    sku = _campos(schema)["armSkuName"]
    assert sku.value_count == len(_VM_SKUS)  # total real
    assert len(sku.values) == catalog.DEFAULT_SAMPLE  # amostra
    assert sku.values_truncated is True
    assert "$filter=" in sku.values_source  # como obter a lista inteira


async def test_sample_size_controla_o_recorte(api_mockada):
    schema = await catalog.config_schema("vm", region="eastus", sample_size=3)
    assert len(_campos(schema)["armSkuName"].values) == 3


async def test_example_config_usa_valores_realmente_enumerados(api_mockada):
    schema = await catalog.config_schema("vm", region="eastus")
    campos = _campos(schema)
    exemplo = schema.example_config
    assert exemplo["armSkuName"] in campos["armSkuName"].values + _VM_SKUS
    assert exemplo["region"] == "eastus"


async def test_regiao_e_normalizada(api_mockada):
    schema = await catalog.config_schema("vm", region="East US")
    assert schema.region == "eastus"


async def test_aceita_key_service_name_e_alias(api_mockada):
    for entrada in ("vm", "Virtual Machines", "máquina virtual"):
        schema = await catalog.config_schema(entrada, region="eastus")
        assert schema.key == "vm"


async def test_servico_desconhecido_levanta_value_error(api_mockada):
    # "kubernetes" servia de exemplo aqui até 21/08, quando virou alias de
    # 'aks'. Trocado por um serviço que segue sem resolver — o teste é sobre a
    # forma do erro, não sobre este serviço em particular.
    with pytest.raises(ValueError, match="Serviço desconhecido"):
        await catalog.config_schema("cosmos db", region="eastus")


async def test_sql_deriva_tier_compute_e_vcores_da_api(api_mockada):
    schema = await catalog.config_schema("sql", region="eastus")
    campos = _campos(schema)
    assert set(campos) == {
        "region",
        "tier",
        "compute",
        "hardware",
        "vCores",
        "priceType",
    }
    assert "General Purpose" in campos["tier"].values
    assert "Business Critical" in campos["tier"].values
    assert campos["compute"].values == ["Provisioned", "Serverless"]
    assert campos["hardware"].values == ["Gen5"]
    assert campos["vCores"].values == _SQL_VCORES
    assert schema.example_config["vCores"] == 2


async def test_storage_deriva_tier_e_redundancia_do_sku_de_capacidade(api_mockada):
    schema = await catalog.config_schema("storage", region="eastus")
    campos = _campos(schema)
    assert campos["tier"].values == ["Archive", "Cool", "Hot"]
    assert campos["redundancy"].values == ["GRS", "LRS", "RA-GRS"]
    assert schema.example_config["tier"] == "Hot"
    assert schema.example_config["redundancy"] == "LRS"


async def test_schema_serializa_para_dict(api_mockada):
    dumped = (await catalog.config_schema("vm", region="eastus")).model_dump()
    assert dumped["service_name"] == "Virtual Machines"
    assert isinstance(dumped["fields"], list)
    assert isinstance(dumped["fields"][0], dict)
    assert dumped["example_config"]["region"] == "eastus"


def test_catalogo_cobre_exatamente_os_resolvers():
    """Toda key devolvida pela busca precisa existir em RESOLVERS, e vice-versa."""
    assert set(catalog._CATALOG) == set(RESOLVERS)


# --------------------------------------------------------------------------- #
# Integração: a cadeia search -> schema -> resolve_price contra a API real.
# --------------------------------------------------------------------------- #
@pytest.mark.integration
async def test_cadeia_search_schema_resolve_price(has_network):
    if not has_network:
        pytest.skip("sem rede: pulando teste de integração")

    matches = await catalog.search_services("máquina virtual")
    assert matches
    key = matches[0].key

    schema = await catalog.config_schema(key, region="eastus")
    campos = _campos(schema)
    assert "Standard_D2s_v3" in campos["armSkuName"].values or (
        campos["armSkuName"].value_count > 0
    )
    assert "eastus" in campos["region"].values

    # A config de exemplo do schema tem que ser aceita sem PriceResolutionError.
    resultado = await resolve_price(key, schema.example_config)
    assert resultado.unit_price > 0
