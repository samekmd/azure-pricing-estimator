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
from azure_estimator_mcp.azure.pricing import monthly_cost, resolve_price
from azure_estimator_mcp.azure.retail_client import BASE_URL, PAGE_SIZE, RetailPricesClient

# tests/ -> mcp-server/ -> raiz do repo
PATTERNS_DIR = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skill"
    / "azure-arch-estimator"
    / "patterns"
)

# Serviços que a Skill trata como BLOQUEADOS de propósito (Fase 4).
BLOQUEADOS = {"synapse"}


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
    assert len(RESOLVIVEIS) == 9
    assert len(BLOQUEADOS_COMPS) == 1


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
@pytest.mark.parametrize(
    ("caso_id", "comp"), [pytest.param(cid, c, id=cid) for cid, c in BLOQUEADOS_COMPS]
)
@respx.mock
async def test_componente_bloqueado_falha_limpo(caso_id, comp):
    service = comp["service"]
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


@pytest.mark.parametrize(
    ("caso_id", "comp"), [pytest.param(cid, c, id=cid) for cid, c in BLOQUEADOS_COMPS]
)
def test_componente_bloqueado_continua_sem_resolver(caso_id, comp):
    """Trava que aks/synapse seguem SEM resolver (Fase 4).

    Quando a Fase 4 implementar um deles, este teste falha de propósito: é o
    lembrete de mover o componente para o conjunto resolvível e travá-lo com
    meter esperado, em vez de deixá-lo só "não mais bloqueado".
    """
    assert comp["service"] not in RESOLVERS
    assert comp["service"] in BLOQUEADOS


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
        await resolve_price("synapse", {"region": "East US"})

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
@pytest.mark.parametrize(
    ("caso_id", "comp"), [pytest.param(cid, c, id=cid) for cid, c in BLOQUEADOS_COMPS]
)
async def test_componente_bloqueado_na_api_real(caso_id, comp, has_network):
    """Mesmo com rede, o bloqueio continua sendo o erro limpo (não um timeout)."""
    if not has_network:
        pytest.skip("sem rede: pulando testes de integração")

    with pytest.raises(PriceResolutionError, match="Serviço desconhecido"):
        await resolve_price(comp["service"], comp["config"])
