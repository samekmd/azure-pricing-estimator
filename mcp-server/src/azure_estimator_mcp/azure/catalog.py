"""Descoberta de serviços e schema de configuração (Fase 2).

DECISÃO DE DESIGN — consulta AO VIVO à Retail Prices API com cache em memória
por TTL (6h), e não um índice de catálogo pré-construído: o índice exigiria um
passo de build e envelheceria em silêncio, enquanto o cache já garante que cada
sonda bate na API no máximo uma vez por TTL, mantendo as tools longe do rate
limit sem artefato para manter sincronizado.

Este módulo é a camada de descoberta que as tools de server.py apenas adaptam.
Ele não calcula preço nem escolhe meter — descreve os campos que os resolvers de
meters.py consomem e enumera os valores válidos a partir dos Items reais da API,
usando o RetailPricesClient (paginação por $skip derivado de len(items)).
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from ..models import FieldSchema, ServiceConfigSchema, ServiceMatch
from .retail_client import RetailPricesClient, build_filter, normalize_region

CACHE_TTL_SECONDS = 6 * 60 * 60
DEFAULT_REGION = "eastus"
DEFAULT_SAMPLE = 25

# SKU/anchors usados nas sondas baratas. São âncoras de consulta (existem em
# praticamente toda região), não defaults de estimativa.
VM_ANCHOR_SKU = "Standard_D2s_v3"
# Mesmo default de resolve_storage: o Blob Block clássico.
PRODUTO_BLOB_DEFAULT = "Blob Storage"

# Só estes campos dos Items entram no cache — evita segurar megabytes de payload.
_KEEP_FIELDS = (
    "serviceName",
    "serviceFamily",
    "productName",
    "skuName",
    "armSkuName",
    "meterName",
    "armRegionName",
    "type",
    "unitOfMeasure",
    "tierMinimumUnits",
)

_cache: dict[tuple, tuple[float, Any]] = {}


def clear_cache() -> None:
    """Zera o cache de sondas (usado pelos testes e após mudança de moeda)."""
    _cache.clear()


def _cache_get(key: tuple) -> Any | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    stamp, value = hit
    if time.monotonic() - stamp > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: tuple, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


# --------------------------------------------------------------------------- #
# Registro de serviços — aliases pt-BR/en para a busca e a âncora de sondagem.
# O serviceName aqui é o mesmo que os resolvers de meters.py usam no $filter, e
# é sempre RECONFIRMADO contra a API antes de ser devolvido ao agente.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Service:
    key: str  # chave em meters.RESOLVERS
    service_name: str
    label: str
    aliases: tuple[str, ...]
    anchor: Callable[[str], dict[str, str]]  # filtros baratos, dada a região


_CATALOG: dict[str, _Service] = {
    "vm": _Service(
        key="vm",
        service_name="Virtual Machines",
        label="Máquinas Virtuais (Virtual Machines)",
        aliases=(
            "vm",
            "vms",
            "virtual machine",
            "virtual machines",
            "maquina virtual",
            "maquinas virtuais",
            "compute",
            "computacao",
            "servidor",
            "instancia",
            "iaas",
        ),
        anchor=lambda region: {
            "serviceName": "Virtual Machines",
            "armSkuName": VM_ANCHOR_SKU,
            "armRegionName": region,
        },
    ),
    "storage": _Service(
        key="storage",
        service_name="Storage",
        label="Armazenamento (Storage / Blob)",
        aliases=(
            "storage",
            "armazenamento",
            "blob",
            "blob storage",
            "objeto",
            "disco",
            "disk",
            "arquivo",
            "file share",
            "data lake",
        ),
        anchor=lambda region: {
            "serviceName": "Storage",
            "armRegionName": region,
            "productName": PRODUTO_BLOB_DEFAULT,
            "skuName": "Hot LRS",
        },
    ),
    "sql": _Service(
        key="sql",
        service_name="SQL Database",
        label="Banco de Dados SQL (SQL Database)",
        aliases=(
            "sql",
            "sql database",
            "azure sql",
            "sql server",
            "mssql",
            "banco de dados",
            "bancos de dados",
            "database",
            "relacional",
        ),
        anchor=lambda region: {
            "serviceName": "SQL Database",
            "armRegionName": region,
            "skuName": "2 vCore",
        },
    ),
    "aks": _Service(
        key="aks",
        service_name="Azure Kubernetes Service",
        label="Kubernetes gerenciado (Azure Kubernetes Service)",
        aliases=(
            "aks",
            "kubernetes",
            "k8s",
            "cluster",
            "clusters",
            "container orchestration",
            "orquestracao de containers",
            "microsservicos",
            "microservicos",
            "microservices",
        ),
        anchor=lambda region: {
            "serviceName": "Azure Kubernetes Service",
            "armRegionName": region,
            "skuName": "Standard",
        },
    ),
}

# Sonda global de regiões: um único SKU de VM em TODAS as regiões devolve a lista
# de armRegionName realmente praticada pela API, numa página só. As regiões são
# as mesmas para os três serviços, então vale a pena não repetir a sondagem.
_REGION_PROBE = {
    "serviceName": "Virtual Machines",
    "armSkuName": VM_ANCHOR_SKU,
    "priceType": "Consumption",
}


# --------------------------------------------------------------------------- #
# Sondagem + cache
# --------------------------------------------------------------------------- #
def _project(item: dict) -> dict:
    return {k: item[k] for k in _KEEP_FIELDS if k in item}


async def _probe(
    filters: dict[str, str],
    *,
    currency: str,
    client: RetailPricesClient,
    top: int | None = None,
) -> list[dict]:
    """Consulta (com cache por TTL) os Items de um conjunto de filtros."""
    key = ("probe", tuple(sorted(filters.items())), currency, top)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    items = [
        _project(it)
        for it in await client.query_prices(dict(filters), currency=currency, top=top)
    ]
    _cache_set(key, items)
    return items


def _distinct(items: list[dict], field: str) -> list[str]:
    return sorted({str(it[field]) for it in items if it.get(field)})


def _source(filters: dict[str, str], field: str) -> str:
    """Descreve de onde vieram os valores, para o agente buscar a lista completa."""
    return f"GET /api/retail/prices?$filter={build_filter(filters)} → campo '{field}'"


def _field(
    name: str,
    type_: str,
    required: bool,
    description: str,
    *,
    default: Any | None = None,
    values: list[Any] | None = None,
    sample: int = DEFAULT_SAMPLE,
    source: str | None = None,
) -> FieldSchema:
    """Monta um FieldSchema, recortando enumerações grandes para não estourar."""
    if values is None:
        return FieldSchema(
            name=name,
            type=type_,
            required=required,
            description=description,
            default=default,
            values_source=source,
        )
    vals = list(values)
    return FieldSchema(
        name=name,
        type=type_,
        required=required,
        description=description,
        default=default,
        values=vals[:sample],
        value_count=len(vals),
        values_truncated=len(vals) > sample,
        values_source=source,
    )


def _prefer(values: list[Any], favorito: Any) -> Any:
    """Usa o valor preferido se ele existir de fato; senão, o primeiro disponível."""
    if favorito in values:
        return favorito
    return values[0] if values else favorito


# --------------------------------------------------------------------------- #
# TOOL 1 — busca de serviços
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    """Minúsculas, sem acento e com espaços colapsados."""
    decomposto = unicodedata.normalize("NFKD", text.strip().lower())
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.split())


def _score(query: str, svc: _Service) -> tuple[int, str]:
    """Pontua o casamento da busca com um serviço: 3=exato, 2=contém, 1=parcial."""
    melhor = (0, "")
    termos = (*svc.aliases, _norm(svc.service_name), _norm(svc.label))
    for termo in termos:
        alvo = _norm(termo)
        if not alvo:
            continue
        if query == alvo:
            pontos = 3
        elif len(query) >= 3 and alvo in query:
            pontos = 2
        elif len(query) >= 3 and query in alvo:
            pontos = 1
        else:
            continue
        if pontos > melhor[0]:
            melhor = (pontos, termo)
    return melhor


async def search_services(
    query: str, *, currency: str = "USD", client: RetailPricesClient | None = None
) -> list[ServiceMatch]:
    """Serviços Azure candidatos para um termo em linguagem natural.

    O serviceName devolvido vem do Item real da API (sonda de 1 item por
    serviço): candidato que a API não confirma não é devolvido. Termo sem
    correspondência devolve lista vazia — nunca erro.
    """
    q = _norm(query or "")
    if not q:
        return []

    candidatos = []
    for svc in _CATALOG.values():
        pontos, termo = _score(q, svc)
        if pontos:
            candidatos.append((pontos, svc, termo))
    if not candidatos:
        return []
    candidatos.sort(key=lambda c: (-c[0], c[1].label))

    owns_client = client is None
    client = client or RetailPricesClient()
    try:
        achados: list[ServiceMatch] = []
        for _pontos, svc, termo in candidatos:
            items = await _probe(
                svc.anchor(DEFAULT_REGION), currency=currency, client=client, top=1
            )
            if not items:  # a API não confirma esse serviceName — não devolve
                continue
            item = items[0]
            achados.append(
                ServiceMatch(
                    key=svc.key,
                    service_name=str(item.get("serviceName") or svc.service_name),
                    label=svc.label,
                    service_family=item.get("serviceFamily"),
                    matched_on=termo,
                )
            )
        return achados
    finally:
        if owns_client:
            await client.aclose()


# --------------------------------------------------------------------------- #
# TOOL 2 — schema de configuração
# --------------------------------------------------------------------------- #
def _lookup(service: str) -> _Service:
    """Aceita a chave do resolver ('vm'), o serviceName ou um alias exato."""
    alvo = _norm(service or "")
    for svc in _CATALOG.values():
        if alvo in {svc.key, _norm(svc.service_name)} or alvo in {
            _norm(a) for a in svc.aliases
        }:
            return svc
    conhecidos = ", ".join(f"{s.key} ({s.service_name})" for s in _CATALOG.values())
    raise ValueError(f"Serviço desconhecido: {service!r}. Conhecidos: {conhecidos}")


async def _regions(*, currency: str, client: RetailPricesClient) -> list[str]:
    items = await _probe(_REGION_PROBE, currency=currency, client=client)
    return _distinct(items, "armRegionName")


async def _fields_vm(*, region, currency, sample_size, client):
    svc = _CATALOG["vm"]
    skus_probe = {
        "serviceName": svc.service_name,
        "armRegionName": region,
        "priceType": "Consumption",
    }
    tipos_probe = svc.anchor(region)

    itens = await _probe(skus_probe, currency=currency, client=client)
    tipos = await _probe(tipos_probe, currency=currency, client=client)
    regioes = await _regions(currency=currency, client=client)
    arm_skus = _distinct(itens, "armSkuName")

    campos = [
        _field(
            "armSkuName",
            "string",
            True,
            "Tamanho da VM no formato ARM (ex.: Standard_D2s_v3).",
            values=arm_skus,
            sample=sample_size,
            source=_source(skus_probe, "armSkuName"),
        ),
        _field(
            "region",
            "string",
            True,
            "Região Azure. Aceita 'East US' ou 'eastus' — é normalizada.",
            values=regioes,
            sample=sample_size,
            source=_source(_REGION_PROBE, "armRegionName"),
        ),
        _field(
            "priceType",
            "string",
            False,
            "Tipo de preço do meter.",
            default="Consumption",
            values=_distinct(tipos, "type"),
            sample=sample_size,
            source=_source(tipos_probe, "type"),
        ),
        _field(
            "windows",
            "boolean",
            False,
            "True para imagem Windows, False (default) para Linux. É flag do "
            "resolver (filtra productName contendo 'Windows'), não vem da API.",
            default=False,
            values=[False, True],
            sample=sample_size,
        ),
    ]
    exemplo = {
        "armSkuName": _prefer(arm_skus, VM_ANCHOR_SKU),
        "region": region,
        "priceType": "Consumption",
    }
    notas = [
        "Variantes Spot e Low Priority são descartadas pelo resolver.",
        "Preço por hora: use estimate_monthly_cost com usage={'hours': N}.",
    ]
    return campos, exemplo, notas


async def _fields_storage(*, region, currency, sample_size, client):
    svc = _CATALOG["storage"]
    base_probe = {
        "serviceName": svc.service_name,
        "armRegionName": region,
        "priceType": "Consumption",
    }
    tipos_probe = svc.anchor(region)

    itens = await _probe(base_probe, currency=currency, client=client)
    tipos = await _probe(tipos_probe, currency=currency, client=client)
    regioes = await _regions(currency=currency, client=client)

    # tier e redundancy saem do skuName dos meters de capacidade ("Data Stored")
    # do produto default — é exatamente o "<tier> <redundancy>" que
    # resolve_storage remonta. Escopar no produto evita oferecer combinações que
    # existem em outro produto (Files, Data Lake) e falhariam no resolver.
    tiers: set[str] = set()
    redundancias: set[str] = set()
    for it in itens:
        if str(it.get("productName")) != PRODUTO_BLOB_DEFAULT:
            continue
        if not str(it.get("meterName", "")).endswith("Data Stored"):
            continue
        partes = str(it.get("skuName", "")).split(" ", 1)
        if len(partes) == 2:
            tiers.add(partes[0])
            redundancias.add(partes[1])
    produtos = _distinct(itens, "productName")

    campos = [
        _field(
            "region",
            "string",
            True,
            "Região Azure. Aceita 'East US' ou 'eastus' — é normalizada.",
            values=regioes,
            sample=sample_size,
            source=_source(_REGION_PROBE, "armRegionName"),
        ),
        _field(
            "tier",
            "string",
            False,
            "Camada de acesso do blob.",
            default="Hot",
            values=sorted(tiers),
            sample=sample_size,
            source=_source(base_probe, "skuName (meters '… Data Stored')"),
        ),
        _field(
            "redundancy",
            "string",
            False,
            "Redundância do armazenamento (LRS, GRS, ZRS, RA-GRS…).",
            default="LRS",
            values=sorted(redundancias),
            sample=sample_size,
            source=_source(base_probe, "skuName (meters '… Data Stored')"),
        ),
        _field(
            "productName",
            "string",
            False,
            "Produto de storage. O mesmo skuName existe em vários produtos; o "
            "default fixa o Blob Block clássico. tier/redundancy acima são "
            "enumerados para esse produto.",
            default=PRODUTO_BLOB_DEFAULT,
            values=produtos,
            sample=sample_size,
            source=_source(base_probe, "productName"),
        ),
        _field(
            "priceType",
            "string",
            False,
            "Tipo de preço do meter.",
            default="Consumption",
            values=_distinct(tipos, "type"),
            sample=sample_size,
            source=_source(tipos_probe, "type"),
        ),
    ]
    exemplo = {
        "region": region,
        "tier": _prefer(sorted(tiers), "Hot"),
        "redundancy": _prefer(sorted(redundancias), "LRS"),
        "productName": PRODUTO_BLOB_DEFAULT,
    }
    notas = [
        "O resolver mira o meter de capacidade ('<tier> <redundancy> Data Stored').",
        "Preço por GB/mês: use estimate_monthly_cost com usage={'gb': N}.",
        "'Data Stored' tem faixas de volume; o resolver usa a faixa base.",
    ]
    return campos, exemplo, notas


_SQL_TIERS = ("General Purpose", "Business Critical", "Hyperscale")
_SQL_HARDWARE = ("Gen5", "Gen4", "DC-Series", "Fsv2", "M-Series", "Premium-Series")
_VCORE_RE = re.compile(r"^(\d+) vCore$")


async def _fields_sql(*, region, currency, sample_size, client):
    svc = _CATALOG["sql"]
    base_probe = {
        "serviceName": svc.service_name,
        "armRegionName": region,
        "priceType": "Consumption",
    }
    tipos_probe = svc.anchor(region)

    itens = await _probe(base_probe, currency=currency, client=client)
    tipos = await _probe(tipos_probe, currency=currency, client=client)
    regioes = await _regions(currency=currency, client=client)

    # tier/hardware/compute vivem dentro do productName ("SQL Database Single
    # General Purpose - Compute Gen5"): confirmamos quais realmente aparecem.
    produtos = _distinct(itens, "productName")
    blob = " | ".join(produtos).lower()
    tiers = [t for t in _SQL_TIERS if t.lower() in blob]
    hardware = [h for h in _SQL_HARDWARE if h.lower() in blob]
    compute = ["Provisioned"] + (["Serverless"] if "serverless" in blob else [])
    vcores = sorted(
        {
            int(m.group(1))
            for it in itens
            if (m := _VCORE_RE.match(str(it.get("skuName", ""))))
        }
    )

    campos = [
        _field(
            "region",
            "string",
            True,
            "Região Azure. Aceita 'East US' ou 'eastus' — é normalizada.",
            values=regioes,
            sample=sample_size,
            source=_source(_REGION_PROBE, "armRegionName"),
        ),
        _field(
            "tier",
            "string",
            False,
            "Camada de serviço.",
            default="General Purpose",
            values=tiers,
            sample=sample_size,
            source=_source(base_probe, "productName"),
        ),
        _field(
            "compute",
            "string",
            False,
            "Modelo de compute.",
            default="Provisioned",
            values=compute,
            sample=sample_size,
            source=_source(base_probe, "productName"),
        ),
        _field(
            "hardware",
            "string",
            False,
            "Família de hardware.",
            default="Gen5",
            values=hardware,
            sample=sample_size,
            source=_source(base_probe, "productName"),
        ),
        _field(
            "vCores",
            "integer",
            False,
            "Quantidade de vCores; casa com skuName '<N> vCore' de forma exata.",
            values=vcores,
            sample=sample_size,
            source=_source(base_probe, "skuName"),
        ),
        _field(
            "priceType",
            "string",
            False,
            "Tipo de preço do meter.",
            default="Consumption",
            values=_distinct(tipos, "type"),
            sample=sample_size,
            source=_source(tipos_probe, "type"),
        ),
    ]
    exemplo = {
        "region": region,
        "tier": _prefer(tiers, "General Purpose"),
        "compute": "Provisioned",
        "hardware": _prefer(hardware, "Gen5"),
        "vCores": _prefer(vcores, 2),
    }
    notas = [
        "Meters '… - Free' (dev/test) são descartados pelo resolver.",
        "Sem vCores a consulta costuma sobrar mais de um meter → PriceResolutionError.",
    ]
    return campos, exemplo, notas


async def _fields_aks(*, region, currency, sample_size, client):
    """Campos do control plane do AKS.

    Cobre SÓ a taxa do control plane gerenciado. Os nós do cluster são VMs
    comuns: quem os precifica é o serviço 'vm' — não há nada de AKS neles.
    """
    svc = _CATALOG["aks"]
    base_probe = {
        "serviceName": svc.service_name,
        "armRegionName": region,
        "priceType": "Consumption",
    }
    itens = await _probe(base_probe, currency=currency, client=client)
    regioes = await _regions(currency=currency, client=client)

    # Só os skuName que têm de fato um meter de control plane que o resolver
    # sabe escolher ("Uptime SLA" / "Long Term Support"). Sondado em 21/08:
    # isso deixa "Standard". Ficam de fora "Automatic" (AKS Automatic, que tem
    # meters próprios — "Automatic Hosted Control Plane" e um por categoria de
    # nó) e os "Anyscale …". Listá-los aqui faria o agente montar uma config
    # que o resolver recusa; o schema promete só o que resolve.
    _METERS_DE_CONTROL_PLANE = ("uptime sla", "long term support")
    tiers = sorted(
        {
            str(it.get("skuName"))
            for it in itens
            if any(
                alvo in str(it.get("meterName", "")).lower()
                for alvo in _METERS_DE_CONTROL_PLANE
            )
        }
    )

    campos = [
        _field(
            "region",
            "string",
            True,
            "Região Azure. Aceita 'East US' ou 'eastus' — é normalizada.",
            values=regioes,
            sample=sample_size,
            source=_source(_REGION_PROBE, "armRegionName"),
        ),
        _field(
            "tier",
            "string",
            False,
            "Camada do control plane. O tier gratuito do AKS não tem meter na "
            "API (não há o que cobrar), então só as camadas listadas resolvem.",
            default="Standard",
            values=tiers,
            sample=sample_size,
            source=_source(base_probe, "skuName"),
        ),
        _field(
            "longTermSupport",
            "boolean",
            False,
            "Cobra o adicional de Long Term Support (suporte estendido de "
            "versão do Kubernetes) em vez do Uptime SLA. São meters "
            "diferentes, e o LTS é bem mais caro.",
            default=False,
        ),
        _field(
            "priceType",
            "string",
            False,
            "Tipo de preço.",
            default="Consumption",
            values=_distinct(itens, "type"),
            sample=sample_size,
            source=_source(base_probe, "type"),
        ),
    ]
    exemplo = {"region": region, "tier": _prefer(tiers, "Standard")}
    notas = [
        "Precifica só o control plane. Os NÓS do cluster são VMs comuns — "
        "resolva-os pelo serviço 'vm', com o armSkuName do node pool.",
        "Sob o skuName 'Standard' convivem dois meters ('Uptime SLA' e 'Long "
        "Term Support'); o resolver escolhe pelo campo longTermSupport.",
        "AKS Automatic não é coberto: é outro produto, com meters próprios "
        "('Automatic Hosted Control Plane' e um por categoria de nó).",
    ]
    return campos, exemplo, notas


_BUILDERS: dict[str, Callable] = {
    "vm": _fields_vm,
    "storage": _fields_storage,
    "sql": _fields_sql,
    "aks": _fields_aks,
}


async def config_schema(
    service: str,
    *,
    region: str = DEFAULT_REGION,
    currency: str = "USD",
    sample_size: int = DEFAULT_SAMPLE,
    client: RetailPricesClient | None = None,
) -> ServiceConfigSchema:
    """Campos de config esperados por um serviço e seus valores válidos.

    Enumeração grande é recortada: cada campo traz uma amostra, o total de
    valores distintos e a consulta da API de onde eles saíram.
    """
    svc = _lookup(service)
    region = normalize_region(region)

    owns_client = client is None
    client = client or RetailPricesClient()
    try:
        campos, exemplo, notas = await _BUILDERS[svc.key](
            region=region, currency=currency, sample_size=sample_size, client=client
        )
    finally:
        if owns_client:
            await client.aclose()

    return ServiceConfigSchema(
        key=svc.key,
        service_name=svc.service_name,
        region=region,
        currency=currency,
        fields=campos,
        example_config=exemplo,
        notes=notas,
    )
