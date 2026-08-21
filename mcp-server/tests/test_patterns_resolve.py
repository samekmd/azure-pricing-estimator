"""Validador pattern-driven: cada componente dos padrões da Skill vira um teste.

Fonte de verdade: os YAMLs de .claude/skill/azure-arch-estimator/patterns/.
Cada `components[]` de cada padrão é parametrizado como um caso INDEPENDENTE
(id "padrao::componente"), para uma falha apontar exatamente qual componente
de qual padrão quebrou — e não "os padrões quebraram".

Duas camadas, de propósito:

  1. OFFLINE (respx): um pool de Items sintéticos cheio de DISTRATORES reais
     (Spot, Windows, faixas de volume, 'Zone Redundancy', Serverless...) e um
     mock que interpreta o $filter OData como a API faz. Isso trava o contrato
     INTEIRO do resolver — os filtros server-side E o seletor — sem rede.
  2. INTEGRAÇÃO (@pytest.mark.integration): a mesma config exata contra a API
     real, para pegar mudança de catálogo do lado da Azure.

Os componentes BLOQUEADOS (aks/synapse) não são um detalhe: a Skill depende do
formato do erro deles para sinalizar "não estimável". Por isso a forma do erro
é travada aqui — se ela mudar, a Skill quebraria em silêncio.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from azure_estimator_mcp.azure.meters import RESOLVERS, PriceResolutionError
from azure_estimator_mcp.azure.pricing import (
    monthly_cost,
    resolve_price,
    sql_monthly_cost,
)
from azure_estimator_mcp.azure.retail_client import BASE_URL, PAGE_SIZE, RetailPricesClient

# tests/ -> mcp-server/ -> raiz do repo
PATTERNS_DIR = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skill"
    / "azure-arch-estimator"
    / "patterns"
)

# Serviços de algum padrão que ainda NÃO têm resolver. Vazio desde 21/08, com
# a entrada de `aks` e `synapse`: os 10 componentes dos 3 padrões resolvem.
# A maquinaria fica de pé de propósito — quando um padrão novo trouxer um
# serviço sem resolver, test_componente_tem_forma_esperada o obriga a passar
# por aqui em vez de falhar de um jeito qualquer.
BLOQUEADOS: set[str] = set()

# Serviços que NÃO são dos padrões e seguem sem resolver — o contrato de erro
# que a Skill usa para dizer "não estimável" continua valendo, e precisa de
# alguém para exercitá-lo agora que BLOQUEADOS esvaziou. Databricks está fora
# de escopo por decisão de produto (ver README); Cosmos DB simplesmente não
# foi implementado.
FORA_DE_ESCOPO = ["databricks", "cosmos db"]


# --------------------------------------------------------------------------- #
# Carga dos padrões
# --------------------------------------------------------------------------- #
def _load_components() -> list[tuple[str, dict]]:
    """Devolve [(id_do_caso, componente)] de todos os padrões, ordenado."""
    out: list[tuple[str, dict]] = []
    for path in sorted(PATTERNS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        pattern_id = (data.get("pattern") or {}).get("id", path.stem)
        for comp in data.get("components") or []:
            out.append((f"{pattern_id}::{comp['id']}", comp))
    return out


ALL_COMPONENTS = _load_components()
RESOLVIVEIS = [(cid, c) for cid, c in ALL_COMPONENTS if c["service"] not in BLOQUEADOS]
BLOQUEADOS_COMPS = [(cid, c) for cid, c in ALL_COMPONENTS if c["service"] in BLOQUEADOS]


def _params(pairs: list[tuple[str, dict]]):
    return pytest.mark.parametrize(
        "comp", [pytest.param(c, id=cid) for cid, c in pairs]
    )


def test_padroes_encontrados():
    """Guarda de sanidade: se a Skill mudar de lugar, falha alto.

    Sem isso, um PATTERNS_DIR errado coletaria ZERO casos e a suíte passaria
    verde sem validar nada — exatamente o modo de falha silenciosa que este
    arquivo existe para evitar.
    """
    assert PATTERNS_DIR.is_dir(), f"patterns/ não encontrado em {PATTERNS_DIR}"
    assert len(list(PATTERNS_DIR.glob("*.yaml"))) == 3
    assert len(ALL_COMPONENTS) == 10, [cid for cid, _ in ALL_COMPONENTS]
    assert len(RESOLVIVEIS) == 10
    assert len(BLOQUEADOS_COMPS) == 0


@_params(ALL_COMPONENTS)
def test_componente_tem_forma_esperada(comp):
    """O contrato do YAML que o resto do arquivo (e a Skill) pressupõe."""
    assert isinstance(comp.get("id"), str) and comp["id"]
    assert isinstance(comp.get("service"), str) and comp["service"]
    assert isinstance(comp.get("config"), dict) and comp["config"]
    assert isinstance(comp.get("usage", {}), dict)
    assert isinstance(comp.get("quantity", 1), int) and comp.get("quantity", 1) >= 1
    # Todo service ou tem resolver, ou está declarado como bloqueado. Um service
    # novo fora dessas duas listas é erro de padrão, não de resolver.
    assert comp["service"] in RESOLVERS or comp["service"] in BLOQUEADOS


# --------------------------------------------------------------------------- #
# API falsa: interpreta o $filter OData e devolve os Items que casam.
#
# O mock aplica os `eq` server-side como a API real faz, então o pool abaixo
# pode conter distratores de TODOS os serviços ao mesmo tempo sem contaminar
# uns aos outros — e os filtros do resolver (não só o seletor) ficam travados.
# --------------------------------------------------------------------------- #
# Campo do $filter -> chave correspondente no Item. A API filtra por
# `priceType` mas devolve o valor em `type`; o resto bate 1:1.
_FILTER_FIELD_TO_ITEM_KEY = {"priceType": "type"}


def _parse_odata_filter(expr: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in expr.split(" and "):
        m = re.match(r"^\s*(\w+)\s+eq\s+'(.*)'\s*$", part)
        assert m, f"filtro OData inesperado: {part!r}"
        out[m.group(1)] = m.group(2).replace("''", "'")
    return out


def _item(**overrides) -> dict:
    base = {
        "retailPrice": 1.0,
        "unitPrice": 1.0,
        "currencyCode": "USD",
        "unitOfMeasure": "1 Hour",
        "serviceName": "Virtual Machines",
        "productName": "Virtual Machines Dv3 Series",
        "skuName": "D2s v3",
        "armSkuName": "Standard_D2s_v3",
        "meterId": "meter-generico",
        "meterName": "D2s v3",
        "armRegionName": "eastus",
        "type": "Consumption",
        "isPrimaryMeterRegion": True,
        "tierMinimumUnits": 0.0,
    }
    base.update(overrides)
    return base


def _vm_family(arm_sku: str, sku: str, product: str, preco: float) -> list[dict]:
    """Uma VM e todas as variantes que o resolver precisa descartar.

    isPrimaryMeterRegion=False no item on-demand e True nos Spot reproduz o
    dado real (ver comentário em resolve_vm): filtrar primário cedo mataria o
    item certo.
    """
    comum = {"armSkuName": arm_sku, "serviceName": "Virtual Machines"}
    return [
        _item(**comum, skuName=sku, meterName=sku, productName=product,
              retailPrice=preco, meterId=f"{arm_sku}-linux", isPrimaryMeterRegion=False),
        _item(**comum, skuName=f"{sku} Spot", meterName=f"{sku} Spot", productName=product,
              retailPrice=preco / 10, meterId=f"{arm_sku}-spot"),
        _item(**comum, skuName=f"{sku} Low Priority", meterName=f"{sku} Low Priority",
              productName=product, retailPrice=preco / 8, meterId=f"{arm_sku}-lowpri"),
        _item(**comum, skuName=sku, meterName=sku, productName=f"{product} Windows",
              retailPrice=preco * 2, meterId=f"{arm_sku}-windows", isPrimaryMeterRegion=False),
        _item(**comum, skuName=f"{sku} Spot", meterName=f"{sku} Spot",
              productName=f"{product} Windows", retailPrice=preco,
              meterId=f"{arm_sku}-windows-spot"),
        # Reserva: descartada pelo filtro priceType, não pelo seletor.
        _item(**comum, skuName=sku, meterName=sku, productName=product,
              retailPrice=preco * 0.6, meterId=f"{arm_sku}-reserva", type="Reservation"),
    ]


def _storage_blob(
    sku: str, produto: str, preco: float, meter_id: str, *, primary: bool = True,
    faixas: bool = True, transacoes: bool = True,
) -> list[dict]:
    """Meter de capacidade + faixas de volume + meters de transação."""
    comum = {"serviceName": "Storage", "productName": produto, "skuName": sku,
             "armSkuName": "", "isPrimaryMeterRegion": primary}
    capacidade = {**comum, "unitOfMeasure": "1 GB/Month"}
    out = [
        _item(**capacidade, meterName=f"{sku} Data Stored", retailPrice=preco,
              meterId=meter_id, tierMinimumUnits=0.0),
    ]
    if faixas:
        # Faixas de volume do MESMO meter: o resolver usa a faixa base.
        out += [
            _item(**capacidade, meterName=f"{sku} Data Stored",
                  retailPrice=round(preco * 0.96, 6), meterId=meter_id,
                  tierMinimumUnits=51200.0),
            _item(**capacidade, meterName=f"{sku} Data Stored",
                  retailPrice=round(preco * 0.92, 6), meterId=meter_id,
                  tierMinimumUnits=512000.0),
        ]
    if transacoes:
        # Transações convivem com a capacidade no mesmo sku.
        out += [
            _item(**comum, meterName=f"{sku} Write Operations", unitOfMeasure="10K",
                  retailPrice=0.05, meterId=f"{meter_id}-write"),
            _item(**comum, meterName=f"{sku} Read Operations", unitOfMeasure="10K",
                  retailPrice=0.004, meterId=f"{meter_id}-read"),
            _item(**comum, meterName=f"{sku} Iterative Write Operations",
                  unitOfMeasure="10K", retailPrice=0.1, meterId=f"{meter_id}-iter"),
        ]
    return out


def _sql_compute(produto: str, sku: str, preco: float, meter_id: str) -> dict:
    return _item(
        serviceName="SQL Database",
        productName=produto,
        skuName=sku,
        armSkuName="",
        meterName="vCore",
        unitOfMeasure="1 Hour",
        retailPrice=preco,
        meterId=meter_id,
    )


_GP_GEN5 = "SQL Database Single/Elastic Pool General Purpose - Compute Gen5"

FAKE_ITEMS: list[dict] = [
    # --- VMs dos padrões + distratores -----------------------------------
    *_vm_family("Standard_D2s_v3", "D2s v3", "Virtual Machines Dv3 Series", 0.096),
    *_vm_family("Standard_D4s_v3", "D4s v3", "Virtual Machines Dv3 Series", 0.192),
    *_vm_family("Standard_D8s_v3", "D8s v3", "Virtual Machines Dv3 Series", 0.384),
    # --- Storage: alvo Blob Storage/Hot LRS + vizinhos que confundem ------
    *_storage_blob("Hot LRS", "Blob Storage", 0.0208, "blob-hot-lrs"),
    *_storage_blob("Cool LRS", "Blob Storage", 0.0152, "blob-cool-lrs"),
    *_storage_blob("Archive LRS", "Blob Storage", 0.00099, "blob-archive-lrs"),
    *_storage_blob("Hot GRS", "Blob Storage", 0.0416, "blob-hot-grs"),
    *_storage_blob("Hot ZRS", "Blob Storage", 0.026, "blob-hot-zrs"),
    *_storage_blob("Hot RA-GRS", "Blob Storage", 0.052, "blob-hot-ragrs"),
    # O distrator mais perigoso do storage: o meter "Hot LRS Data Stored"
    # existe em SEIS produtos ao mesmo tempo, vários deles pelo MESMO preço
    # (0.0208) e com isPrimaryMeterRegion=True. Nem o preço nem a flag de
    # região primária separam — só o productName. Produtos, preços e flags
    # abaixo são os que a API devolve hoje para Storage/eastus (sondados, não
    # supostos): sem o filtro de produto, o seletor vê 5+ candidatos.
    *_storage_blob("Hot LRS", "Azure Data Lake Storage Gen2 Hierarchical Namespace",
                   0.0208, "adls-hns-hot-lrs"),
    *_storage_blob("Hot LRS", "Azure Data Lake Storage Gen2 Flat Namespace",
                   0.0208, "adls-fns-hot-lrs"),
    *_storage_blob("Hot LRS", "General Block Blob v2", 0.0208, "gbbv2-hot-lrs",
                   primary=False),
    *_storage_blob("Hot LRS", "General Block Blob v2 Hierarchical Namespace",
                   0.021, "gbbv2-hns-hot-lrs"),
    # Files v2 só tem a faixa base desse meter no dado real.
    *_storage_blob("Hot LRS", "Files v2", 0.0287, "files-hot-lrs", faixas=False),
    # --- SQL: alvo GP/Gen5/2 vCore + vizinhos -----------------------------
    _sql_compute(_GP_GEN5, "2 vCore", 0.304434, "sql-gp-gen5-2vcore"),
    # A API repete a MESMA linha do meter GP/Gen5 (Single vs Elastic Pool):
    # mesmo meterId+preço+faixa. _dedup tem que colapsar isso.
    _sql_compute(_GP_GEN5, "2 vCore", 0.304434, "sql-gp-gen5-2vcore"),
    _sql_compute(_GP_GEN5, "4 vCore", 0.608868, "sql-gp-gen5-4vcore"),
    _sql_compute(_GP_GEN5, "8 vCore", 1.217736, "sql-gp-gen5-8vcore"),
    _sql_compute(_GP_GEN5, "12 vCore", 1.826604, "sql-gp-gen5-12vcore"),
    _sql_compute(_GP_GEN5, "32 vCore", 4.870944, "sql-gp-gen5-32vcore"),
    # Substring de "2 vCore" — só o match EXATO de skuName descarta.
    _sql_compute(_GP_GEN5, "2 vCore Zone Redundancy", 0.152, "sql-gp-gen5-2vcore-zr"),
    _sql_compute(
        "SQL Database Single/Elastic Pool General Purpose - Serverless - Compute Gen5",
        "2 vCore", 0.5218, "sql-gp-serverless-gen5-2vcore",
    ),
    _sql_compute(
        "SQL Database Single/Elastic Pool Business Critical - Compute Gen5",
        "2 vCore", 0.8092, "sql-bc-gen5-2vcore",
    ),
    _sql_compute(
        "SQL Database Single/Elastic Pool General Purpose - Compute DC-Series",
        "2 vCore", 0.4106, "sql-gp-dc-2vcore",
    ),
    _sql_compute(
        "SQL Database Single/Elastic Pool General Purpose - Compute FSv2",
        "8 vCore", 1.1234, "sql-gp-fsv2-8vcore",
    ),
    # Meter gratuito de dev/test: descartado pelo seletor.
    _item(
        serviceName="SQL Database", productName=_GP_GEN5, skuName="2 vCore",
        armSkuName="", meterName="vCore Free", unitOfMeasure="1 Hour",
        retailPrice=0.0, meterId="sql-gp-gen5-2vcore-free",
    ),
    # Armazenamento do SQL: mesmo serviceName, outro eixo de meter.
    _item(
        serviceName="SQL Database",
        productName="SQL Database Single/Elastic Pool General Purpose - Storage",
        skuName="Data Stored", armSkuName="", meterName="Data Stored",
        unitOfMeasure="1 GB/Month", retailPrice=0.115, meterId="sql-gp-storage",
    ),
    # --- Licença do SQL: outro eixo, e SEM região comercial ----------------
    # A licença vive em armRegionName="Global" (não em eastus) e a palavra
    # "License" está no productName, não no meterName — as duas razões de ela
    # ter passado despercebida até 21/08.
    _item(
        serviceName="SQL Database",
        productName="SQL Database Single/Elastic Pool General Purpose - SQL License",
        skuName="vCore", armSkuName="", meterName="vCore",
        unitOfMeasure="1 Hour", retailPrice=0.099966,
        armRegionName="Global", meterId="sql-license-gp",
    ),
    # O GÊMEO PERIGOSO: mesmo meterId, preço 0.00, type=DevTestConsumption.
    # _dedup NÃO o colapsa (a chave inclui o preço), então sem o filtro de
    # priceType sobrariam 2 candidatos — ou pior, o de graça.
    _item(
        serviceName="SQL Database",
        productName="SQL Database Single/Elastic Pool General Purpose - SQL License",
        skuName="vCore", armSkuName="", meterName="vCore",
        unitOfMeasure="1 Hour", retailPrice=0.0, type="DevTestConsumption",
        armRegionName="Global", meterId="sql-license-gp",
    ),
    _item(
        serviceName="SQL Database",
        productName="SQL Database Single/Elastic Pool Business Critical - SQL License",
        skuName="vCore", armSkuName="", meterName="vCore",
        unitOfMeasure="1 Hour", retailPrice=0.375,
        armRegionName="Global", meterId="sql-license-bc",
    ),
    # Gov tem meter próprio, mais caro, noutra pseudo-região.
    _item(
        serviceName="SQL Database",
        productName="SQL Database Single/Elastic Pool General Purpose - SQL License",
        skuName="vCore", armSkuName="", meterName="vCore",
        unitOfMeasure="1 Hour", retailPrice=0.124957,
        armRegionName="US Gov", meterId="sql-license-gp-usgov",
    ),
    # --- AKS: control plane. A ARMADILHA está aqui ------------------------
    # Medido na API real (eastus, 21/08): o meter CERTO ("Standard Uptime SLA")
    # vem com isPrimaryMeterRegion=False, e o adicional de Long Term Support —
    # 6x mais caro — vem com True. Um resolver que filtrasse região primária
    # antes de escolher o meter devolveria o LTS em silêncio. Os valores abaixo
    # reproduzem isso de propósito: se alguém acrescentar _primary_only ao
    # resolve_aks, este pool faz o teste falhar.
    _item(
        serviceName="Azure Kubernetes Service",
        productName="Azure Kubernetes Service", skuName="Standard",
        armSkuName="", meterName="Standard Uptime SLA", unitOfMeasure="1 Hour",
        retailPrice=0.1, meterId="aks-standard-uptime-sla",
        isPrimaryMeterRegion=False,
    ),
    _item(
        serviceName="Azure Kubernetes Service",
        productName="Azure Kubernetes Service", skuName="Standard",
        armSkuName="", meterName="Standard Long Term Support",
        unitOfMeasure="1 Hour", retailPrice=0.6, meterId="aks-standard-lts",
        isPrimaryMeterRegion=True,
    ),
    # AKS Automatic: outro produto, mesmo serviceName. O filtro de skuName o
    # descarta server-side; está aqui para provar que descarta.
    *[
        _item(
            serviceName="Azure Kubernetes Service",
            productName="Azure Kubernetes Service - Automatic",
            skuName="Automatic", armSkuName="", meterName=nome,
            unitOfMeasure="1 Hour", retailPrice=preco,
            meterId=f"aks-automatic-{nome.lower().replace(' ', '-')}",
        )
        for nome, preco in (
            ("Automatic Hosted Control Plane", 0.16),
            ("Automatic General Purpose", 0.007841),
            ("Automatic Compute Optimized", 0.012196),
        )
    ],
    # --- Synapse: serverless SQL pool, cobrado por TB PROCESSADO ----------
    _item(
        serviceName="Azure Synapse Analytics",
        productName="Azure Synapse Analytics Serverless SQL Pool",
        skuName="Standard", armSkuName="", meterName="Standard Data Processed",
        unitOfMeasure="1 TB", retailPrice=5.0,
        meterId="synapse-serverless-data-processed",
    ),
    # O distrator que obriga o match EXATO de productName: "Serverless Apache
    # Spark Pool" também contém "Serverless", mas é outro produto e é cobrado
    # por vCore/HORA. Casar por substring devolveria preço de outra unidade —
    # e monthly_cost projetaria 730h em cima, não 5 TB.
    _item(
        serviceName="Azure Synapse Analytics",
        productName="Azure Synapse Analytics Serverless Apache Spark Pool - Memory Optimized",
        skuName="vCore", armSkuName="", meterName="vCore",
        unitOfMeasure="1 Hour", retailPrice=0.138,
        meterId="synapse-spark-vcore",
    ),
    # Outros eixos do MESMO serviceName, cada um com sua unidade.
    _item(
        serviceName="Azure Synapse Analytics",
        productName="Azure Synapse Analytics Dedicated SQL Pool",
        skuName="DW1000c", armSkuName="", meterName="100 DWUs",
        unitOfMeasure="1/Hour", retailPrice=15.1,
        meterId="synapse-dedicated-dw1000c", isPrimaryMeterRegion=False,
    ),
    _item(
        serviceName="Azure Synapse Analytics",
        productName="Azure Synapse Analytics Storage",
        skuName="Standard RA-GRS", armSkuName="",
        meterName="Standard RA-GRS Data Stored",
        unitOfMeasure="1 GB/Month", retailPrice=0.0562,
        meterId="synapse-storage-ragrs",
    ),
    _item(
        serviceName="Azure Synapse Analytics",
        productName="Azure Synapse Analytics Pipelines",
        skuName="Operations", armSkuName="", meterName="Operations",
        unitOfMeasure="50K", retailPrice=0.25, meterId="synapse-pipelines-ops",
    ),
    # --- Ruído de outra região: descartado pelo filtro de região ----------
    *[
        {**it, "armRegionName": "westeurope", "meterId": f"{it['meterId']}-weu"}
        for it in _vm_family("Standard_D2s_v3", "D2s v3", "Virtual Machines Dv3 Series", 0.11)
    ],
]


def _fake_api(request: httpx.Request) -> httpx.Response:
    """Responde como a Retail Prices API: aplica os `eq` do $filter."""
    params = request.url.params
    wanted = _parse_odata_filter(params["$filter"])
    items = [
        it
        for it in FAKE_ITEMS
        if all(
            str(it.get(_FILTER_FIELD_TO_ITEM_KEY.get(field, field))) == value
            for field, value in wanted.items()
        )
    ]
    skip = int(params.get("$skip", 0))
    page = items[skip : skip + PAGE_SIZE]
    return httpx.Response(200, json={"Items": page, "NextPageLink": None})


@pytest.fixture
def api_falsa():
    """Cliente apontado para a API falsa (offline)."""
    with respx.mock:
        respx.get(BASE_URL).mock(side_effect=_fake_api)
        yield RetailPricesClient()


# Meter que CADA componente resolvível deve isolar. É a trava de fato: sem
# isso o teste só provaria "resolveu algo", não "resolveu o certo".
METER_ESPERADO = {
    "three-tier-web-app::web-tier": "Standard_D2s_v3-linux",
    "three-tier-web-app::app-tier": "Standard_D2s_v3-linux",
    "three-tier-web-app::data-tier": "sql-gp-gen5-2vcore",
    "three-tier-web-app::static-assets": "blob-hot-lrs",
    "aks-microservices::node-pool": "Standard_D4s_v3-linux",
    "aks-microservices::database": "sql-gp-gen5-2vcore",
    "aks-microservices::shared-storage": "blob-hot-lrs",
    "aks-microservices::control-plane-sla": "aks-standard-uptime-sla",
    "data-lakehouse::data-lake-storage": "blob-hot-lrs",
    "data-lakehouse::synapse-serverless-sql": "synapse-serverless-data-processed",
}


# --------------------------------------------------------------------------- #
# PASSO 2a — componentes RESOLVÍVEIS, offline (respx)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("caso_id", "comp"), [pytest.param(cid, c, id=cid) for cid, c in RESOLVIVEIS]
)
async def test_componente_resolvivel_isola_um_meter(caso_id, comp, api_falsa):
    """A config EXATA do YAML isola exatamente um meter — e o meter certo."""
    async with api_falsa as client:
        try:
            price = await resolve_price(comp["service"], comp["config"], client=client)
        except PriceResolutionError as exc:
            pytest.fail(f"{caso_id}: config do padrão não isolou um único meter: {exc}")

    assert price.unit_price > 0
    assert price.meter_id == METER_ESPERADO[caso_id], (
        f"{caso_id}: resolveu o meter errado "
        f"({price.meter_id} / sku={price.sku_name!r} / meter={price.meter_name!r})"
    )
    # O usage do próprio YAML tem que ser projetável: unidade sem mapeamento em
    # monthly_cost torna o componente não estimável, mesmo resolvendo o preço.
    assert monthly_cost(price, comp.get("usage") or {}) > 0


# --------------------------------------------------------------------------- #
# PASSO 2b — componentes BLOQUEADOS: a forma do erro que a Skill captura.
#
# A Skill usa ESTE erro para marcar o componente como "não estimável". Se o
# tipo, a mensagem ou o caminho mudarem, ela para de reconhecer o bloqueio e
# quebra em silêncio — por isso cada aspecto está travado abaixo.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("service", FORA_DE_ESCOPO)
@respx.mock
async def test_servico_sem_resolver_falha_limpo(service):
    comp = {"config": {"region": "East US"}}
    # respx SEM rotas: qualquer tentativa de HTTP estoura. O bloqueio tem que
    # ser detectado ANTES de qualquer rede — é o que o torna barato e estável.
    with pytest.raises(PriceResolutionError) as exc_info:
        await resolve_price(service, comp["config"])

    exc = exc_info.value
    msg = str(exc)
    assert "Serviço desconhecido" in msg, msg
    assert repr(service) in msg, msg
    # A mensagem lista o que existe hoje, para o chamador se orientar.
    assert "Conhecidos" in msg and "'vm'" in msg, msg
    # Serviço desconhecido não é ambiguidade de meter: não há candidatos.
    assert exc.candidates == []


@_params(ALL_COMPONENTS)
def test_todo_componente_dos_padroes_tem_resolver(comp):
    """A guarda INVERSA da que existia aqui até 21/08.

    Antes, este teste travava que `aks` e `synapse` seguiam SEM resolver, e
    falhava de propósito quando fossem implementados — foi exatamente o que
    aconteceu. Agora que os 10 componentes resolvem, o que vale a pena travar
    é o contrário: nenhum componente pode REGREDIR para "sem resolver", e um
    padrão novo que traga serviço não suportado tem que declará-lo em
    BLOQUEADOS conscientemente, não escorregar despercebido.
    """
    assert comp["service"] in RESOLVERS, (
        f"{comp['id']}: serviço {comp['service']!r} sem resolver. Se for "
        "intencional, declare-o em BLOQUEADOS."
    )

    # E os que sobraram em FORA_DE_ESCOPO seguem fora, senão o contrato de
    # erro que a Skill usa deixaria de ser exercitado por alguém.
    for service in FORA_DE_ESCOPO:
        assert service not in RESOLVERS


@respx.mock
async def test_erro_de_bloqueio_se_distingue_de_erro_de_ambiguidade():
    """Os dois PriceResolutionError que a Skill trata de formas diferentes.

    Serviço bloqueado (não há resolver) e config ambígua (o resolver existe,
    mas sobrou mais de um meter) são o MESMO tipo de exceção. A Skill reage
    diferente a cada um — bloqueado vira "não estimável", ambíguo vira "revise
    a config" — então mensagem e `candidates` precisam continuar separando os
    dois casos.
    """
    respx.get(BASE_URL).mock(side_effect=_fake_api)

    with pytest.raises(PriceResolutionError) as bloqueado:
        await resolve_price(FORA_DE_ESCOPO[0], {"region": "East US"})

    # Mesmo serviço válido (sql), mas sem vCores: sobra mais de um meter.
    async with RetailPricesClient() as client:
        with pytest.raises(PriceResolutionError) as ambiguo:
            await resolve_price(
                "sql", {"region": "East US", "tier": "General Purpose"}, client=client
            )

    assert "Serviço desconhecido" in str(bloqueado.value)
    assert bloqueado.value.candidates == []

    assert "Múltiplos meters" in str(ambiguo.value)
    assert len(ambiguo.value.candidates) > 1


# --------------------------------------------------------------------------- #
# Consistência entre padrões — a mesma config repetida em padrões diferentes
# tem que cair no mesmo meter, e o volume não pode influenciar a resolução.
# --------------------------------------------------------------------------- #
def _comp(caso_id: str) -> dict:
    return next(c for cid, c in ALL_COMPONENTS if cid == caso_id)


async def test_sql_gp_gen5_2vcore_consistente_entre_padroes(api_falsa):
    """SQL GP/Provisioned/Gen5/2 vCores aparece em dois padrões: mesmo meter."""
    a = _comp("three-tier-web-app::data-tier")
    b = _comp("aks-microservices::database")
    assert a["config"] == b["config"]

    async with api_falsa as client:
        pa = await resolve_price("sql", a["config"], client=client)
        pb = await resolve_price("sql", b["config"], client=client)

    assert pa.meter_id == pb.meter_id
    assert pa.unit_price == pb.unit_price
    assert pa.sku_name == "2 vCore"  # não "2 vCore Zone Redundancy"


async def test_storage_volume_nao_afeta_resolucao(api_falsa):
    """Os 3 storages (200/500/1000 GB) resolvem o MESMO meter.

    O volume vive em `usage`, não em `config` — quem muda a resolução é a
    combinação tier/redundancy/produto. Só o custo mensal deve variar.
    """
    casos = [
        ("aks-microservices::shared-storage", 200),
        ("three-tier-web-app::static-assets", 500),
        ("data-lakehouse::data-lake-storage", 1000),
    ]
    resultados = []
    async with api_falsa as client:
        for caso_id, gb in casos:
            comp = _comp(caso_id)
            assert comp["usage"]["gb"] == gb
            price = await resolve_price("storage", comp["config"], client=client)
            resultados.append((price, monthly_cost(price, comp["usage"])))

    meters = {p.meter_id for p, _ in resultados}
    assert meters == {"blob-hot-lrs"}, meters
    # Custo cresce com o volume, na proporção do preço unitário.
    custos = [c for _, c in resultados]
    assert custos == sorted(custos) and custos[0] < custos[-1]
    assert custos[1] == pytest.approx(resultados[1][0].unit_price * 500)


async def test_storage_nao_pega_meter_de_outro_produto(api_falsa):
    """Trava o distrator mais perigoso do storage.

    "Hot LRS Data Stored" existe em 6 produtos ao mesmo tempo (Blob Storage,
    ADLS Gen2 Hierarchical/Flat Namespace, General Block Blob v2 e sua variante
    HNS, Files v2) — e vários pelo MESMO preço, com a mesma flag de região
    primária. Sem o filtro de produto sobram 5+ candidatos; com um filtro por
    substring, um produto novo cujo nome contenha "Blob Storage" passaria a
    casar em silêncio. Daí o match EXATO em resolve_storage.
    """
    comp = _comp("data-lakehouse::data-lake-storage")
    async with api_falsa as client:
        price = await resolve_price("storage", comp["config"], client=client)
    assert price.meter_id == "blob-hot-lrs"
    assert price.unit_of_measure == "1 GB/Month"  # capacidade, não transação


async def test_vm_do_node_pool_nao_pega_spot_nem_windows(api_falsa):
    """D4s_v3 (só exercitado a partir dos padrões) descarta Spot/Windows.

    O item on-demand tem isPrimaryMeterRegion=False e os Spot têm True — o
    caso que obriga resolve_vm a excluir variantes ANTES do desempate por
    região primária.
    """
    comp = _comp("aks-microservices::node-pool")
    async with api_falsa as client:
        price = await resolve_price("vm", comp["config"], client=client)
    assert price.meter_id == "Standard_D4s_v3-linux"
    assert "Spot" not in price.sku_name and "Low Priority" not in price.sku_name
    assert price.unit_price == 0.192  # preço Linux on-demand, não Windows/Spot


# --------------------------------------------------------------------------- #
# PASSO 2c — os mesmos componentes contra a API REAL (puláveis offline).
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.parametrize(
    ("caso_id", "comp"), [pytest.param(cid, c, id=cid) for cid, c in RESOLVIVEIS]
)
async def test_componente_resolvivel_na_api_real(caso_id, comp, has_network):
    if not has_network:
        pytest.skip("sem rede: pulando testes de integração")

    try:
        price = await resolve_price(comp["service"], comp["config"])
    except PriceResolutionError as exc:
        pytest.fail(f"{caso_id}: config do padrão não isolou um único meter: {exc}")

    assert price.unit_price > 0
    assert price.region == "eastus"
    custo = monthly_cost(price, comp.get("usage") or {})
    assert custo > 0
    # Faixa de sanidade larga: pega um meter absurdo (preço de outro produto ou
    # de outra unidade) sem quebrar a cada reajuste de preço da Azure.
    assert custo < 10_000, f"{caso_id}: custo mensal implausível: {custo}"


@pytest.mark.integration
@pytest.mark.parametrize("service", FORA_DE_ESCOPO)
async def test_servico_sem_resolver_na_api_real(service, has_network):
    """Mesmo com rede, o bloqueio continua sendo o erro limpo (não um timeout)."""
    if not has_network:
        pytest.skip("sem rede: pulando testes de integração")

    with pytest.raises(PriceResolutionError, match="Serviço desconhecido"):
        await resolve_price(service, {"region": "East US"})


@respx.mock
async def test_synapse_nao_pega_o_spark_pool_nem_o_dedicated(api_falsa):
    """O serviceName do Synapse cobre eixos de cobrança incompatíveis.

    Sob "Azure Synapse Analytics" convivem serverless SQL (TB processado),
    Spark pool (vCore/hora), Dedicated SQL (DWU/hora), Pipelines (operações) e
    Storage (GB/mês). Casar o produto errado não daria erro: daria um NÚMERO,
    calculado com a unidade de outro eixo.
    """
    comp = _comp("data-lakehouse::synapse-serverless-sql")
    async with api_falsa as client:
        price = await resolve_price("synapse", comp["config"], client=client)

    assert price.meter_id == "synapse-serverless-data-processed"
    assert price.unit_of_measure == "1 TB"  # não "1 Hour" do Spark/Dedicated
    assert monthly_cost(price, comp["usage"]) == pytest.approx(25.0)


async def test_synapse_com_tier_sem_resolver_falha_antes_da_rede():
    """Dedicated/Spark/Pipelines param com erro claro, sem gastar chamada."""
    with pytest.raises(PriceResolutionError, match="Serverless SQL Pool"):
        await resolve_price("synapse", {"region": "East US", "tier": "Dedicated SQL Pool"})


# --------------------------------------------------------------------------- #
# Licença do SQL — a pendência aberta desde 20/08, fechada em 21/08.
#
# resolve_sql isola a linha de COMPUTE. A licença é um meter à parte, e sem ela
# estimate_monthly_cost subestimava um banco com licença inclusa em ~66%.
# --------------------------------------------------------------------------- #
async def test_licenca_do_sql_vem_da_pseudo_regiao_global(api_falsa):
    """A licença não tem região comercial — procurá-la em 'eastus' dá zero.

    É a mesma classe de armadilha que _SPECIAL_REGIONS existe para evitar: o
    meter some em silêncio e nada indica que havia mais o que procurar.
    """
    async with api_falsa as client:
        price = await resolve_price(
            "sql_license", {"region": "East US", "tier": "General Purpose"},
            client=client,
        )
    assert price.meter_id == "sql-license-gp"
    assert price.unit_price == pytest.approx(0.099966)


async def test_licenca_do_sql_ignora_a_linha_devtest(api_falsa):
    """O gêmeo DevTestConsumption tem o MESMO meterId e custa 0.00.

    _dedup não o colapsa (a chave inclui o preço), então quem o descarta é o
    filtro de priceType. Sem ele, a licença sairia de graça — o mesmo erro por
    baixo que este trabalho veio corrigir.
    """
    async with api_falsa as client:
        price = await resolve_price(
            "sql_license", {"region": "East US"}, client=client
        )
    assert price.unit_price > 0
    assert price.price_type == "Consumption"


async def test_licenca_do_sql_respeita_o_tier(api_falsa):
    async with api_falsa as client:
        bc = await resolve_price(
            "sql_license", {"region": "East US", "tier": "Business Critical"},
            client=client,
        )
    assert bc.meter_id == "sql-license-bc"
    assert bc.unit_price == pytest.approx(0.375)


async def test_licenca_do_sql_em_regiao_gov_usa_o_meter_proprio(api_falsa):
    """Gov é a única exceção ao 'Global' — e é mais cara."""
    async with api_falsa as client:
        price = await resolve_price(
            "sql_license", {"region": "usgovvirginia"}, client=client
        )
    assert price.meter_id == "sql-license-gp-usgov"


async def test_licenca_com_tier_sem_mapeamento_falha_claro():
    with pytest.raises(PriceResolutionError, match="linha de licença"):
        await resolve_price("sql_license", {"region": "East US", "tier": "Basic"})


async def test_custo_do_sql_soma_compute_e_licenca(api_falsa):
    """O número que a Fase 2 mediu na calculadora: 222.24 + 145.95 = 368.19."""
    comp = _comp("three-tier-web-app::data-tier")
    async with api_falsa as client:
        breakdown = await sql_monthly_cost(comp["config"], comp.get("usage") or {},
                                           client=client)

    assert breakdown["compute"] == pytest.approx(222.24, abs=0.01)
    assert breakdown["license"] == pytest.approx(145.95, abs=0.01)
    assert breakdown["total"] == pytest.approx(368.19, abs=0.01)
    # A licença é por vCore/hora: 2 vCores dobram a linha, o compute não.
    assert breakdown["license"] == pytest.approx(
        0.099966 * 730 * comp["config"]["vCores"], abs=0.01
    )


async def test_azure_hybrid_benefit_nao_cobra_licenca(api_falsa):
    """licenseIncluded=False espelha o softwareBillingOption=BYOL da UI."""
    comp = _comp("three-tier-web-app::data-tier")
    config = {**comp["config"], "licenseIncluded": False}
    async with api_falsa as client:
        breakdown = await sql_monthly_cost(config, client=client)

    assert breakdown["license"] == 0.0
    assert breakdown["total"] == pytest.approx(breakdown["compute"])
    assert breakdown["license_meter_id"] is None


async def test_custo_do_sql_exige_vcores_para_a_licenca(api_falsa):
    """Sem vCores não dá para calcular uma cobrança POR vCore — para em vez de chutar."""
    async with api_falsa as client:
        with pytest.raises(ValueError, match="vCores"):
            await sql_monthly_cost(
                {"region": "East US", "tier": "General Purpose"}, client=client
            )


@pytest.mark.integration
async def test_custo_do_sql_na_api_real_bate_com_a_calculadora(has_network):
    """Contra a API real: o total do data-tier bate com a calculadora.

    Referência medida na calculadora oficial em 20/08 para esta config exata
    (GP / Gen5 / 2 vCore / East US): Compute $222.24 + License $145.95 =
    $368.19. As faixas são largas o bastante para sobreviver a reajuste de
    preço da Azure, mas estreitas o bastante para pegar meter errado.
    """
    if not has_network:
        pytest.skip("sem rede: pulando testes de integração")

    comp = _comp("three-tier-web-app::data-tier")
    breakdown = await sql_monthly_cost(comp["config"], comp.get("usage") or {})

    assert breakdown["compute"] > 0 and breakdown["license"] > 0
    assert breakdown["total"] == pytest.approx(
        breakdown["compute"] + breakdown["license"]
    )
    # A licença é ~66% do compute nesta config — se virar 0% ou 300%, o meter
    # escolhido mudou de natureza.
    proporcao = breakdown["license"] / breakdown["compute"]
    assert 0.4 < proporcao < 1.0, f"proporção licença/compute implausível: {proporcao}"
    assert 250 < breakdown["total"] < 600, breakdown
