"""Lógica config->meter por serviço.

Cada resolver recebe uma config e devolve:
  - um dict de filtros OData (aplicados server-side pelo retail_client), e
  - uma função de seleção que, dada a lista de Items retornada, isola o ÚNICO
    item correto (aplicando desambiguações que não dá para expressar no $filter,
    como exclusão de variantes Spot/Windows por substring).

Regra dura: se após os filtros sobrarem 0 ou >1 candidatos plausíveis, o
seletor NÃO adivinha — levanta PriceResolutionError com os candidatos, para o
chamador decidir.
"""

from __future__ import annotations

from typing import Callable

# (filtros OData, seletor) — o seletor recebe os Items e devolve um único item.
Resolver = tuple[dict[str, str], Callable[[list[dict]], dict]]


class PriceResolutionError(Exception):
    """Levantada quando um resolver não consegue isolar exatamente um meter."""

    def __init__(self, message: str, candidates: list[dict] | None = None) -> None:
        self.candidates = candidates or []
        # Anexa um resumo dos candidatos à mensagem, sem poluir demais.
        if self.candidates:
            resumo = ", ".join(
                f"{c.get('skuName')!r}/{c.get('meterName')!r}"
                for c in self.candidates[:8]
            )
            extra = "" if len(self.candidates) <= 8 else f" (+{len(self.candidates) - 8})"
            message = f"{message} | {len(self.candidates)} candidatos: {resumo}{extra}"
        super().__init__(message)


def _contains(item: dict, field: str, needle: str) -> bool:
    return needle.lower() in str(item.get(field, "")).lower()


def _primary_only(items: list[dict]) -> list[dict]:
    """Mantém só meters da região primária, salvo se nenhum tiver a flag."""
    primary = [it for it in items if it.get("isPrimaryMeterRegion") is True]
    return primary if primary else items


def _dedup(items: list[dict]) -> list[dict]:
    """Colapsa linhas idênticas do MESMO meter.

    A API às vezes devolve o mesmo meter duplicado (ex.: SQL "Single/Elastic
    Pool" repetido). Linhas com mesmo meterId + preço + faixa são a mesma oferta;
    faixas de volume (tierMinimumUnits distinto) e preços diferentes são preservados.
    """
    seen: set = set()
    out: list[dict] = []
    for it in items:
        key = (it.get("meterId"), it.get("retailPrice"), it.get("tierMinimumUnits"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _select_unique(candidates: list[dict], context: str) -> dict:
    """Garante exatamente um candidato ou levanta PriceResolutionError."""
    candidates = _dedup(candidates)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise PriceResolutionError(f"Nenhum meter encontrado para {context}")
    raise PriceResolutionError(
        f"Múltiplos meters plausíveis para {context}", candidates=candidates
    )


# --------------------------------------------------------------------------- #
# VM — caso limpo: armSkuName + serviceName + region isolam bem.
# --------------------------------------------------------------------------- #
def resolve_vm(config: dict) -> Resolver:
    arm_sku = config["armSkuName"]  # ex.: 'Standard_D2s_v3'
    region = config["region"]
    price_type = config.get("priceType", "Consumption")
    windows = bool(config.get("windows", False))  # default: Linux

    filters = {
        "serviceName": "Virtual Machines",
        "armRegionName": region,
        "armSkuName": arm_sku,
        "priceType": price_type,
    }

    def select(items: list[dict]) -> dict:
        # Exclui variantes indesejadas ANTES do desempate por região primária:
        # no dado real, o meter on-demand de D2s v3 tem isPrimaryMeterRegion=False
        # e só os Spot têm True — filtrar primário cedo eliminaria o item certo.
        cands = [
            it
            for it in items
            if not _contains(it, "meterName", "Spot")
            and not _contains(it, "skuName", "Spot")
            and not _contains(it, "meterName", "Low Priority")
            and not _contains(it, "skuName", "Low Priority")
        ]
        # Windows vs Linux: o preço de VM Windows vem em productName com "Windows".
        if windows:
            cands = [it for it in cands if _contains(it, "productName", "Windows")]
        else:
            cands = [it for it in cands if not _contains(it, "productName", "Windows")]
        cands = _primary_only(cands)  # desempate final, se ainda houver duplicatas
        return _select_unique(cands, f"VM {arm_sku} em {region} ({price_type})")

    return filters, select


# --------------------------------------------------------------------------- #
# Storage — caso BAGUNÇADO: muitos meters (redundância, tier, capacidade vs
# transações). Implementado para Blob Block: redundancy + tier, meter de
# capacidade ("Data Stored").
# --------------------------------------------------------------------------- #
def resolve_storage(config: dict) -> Resolver:
    region = config["region"]
    redundancy = config.get("redundancy", "LRS")  # LRS/GRS/ZRS/RA-GRS...
    tier = config.get("tier", "Hot")  # Hot/Cool/Archive
    # O mesmo skuName (ex.: "Hot LRS") existe em vários produtos (Blob Storage,
    # General Block Blob v2, Data Lake Gen2...). Fixamos "Blob Storage" como o
    # produto Blob Block clássico; sobreponível via config.
    product = config.get("productName", "Blob Storage")
    price_type = config.get("priceType", "Consumption")

    sku = f"{tier} {redundancy}"  # ex.: "Hot LRS"
    meter = f"{tier} {redundancy} Data Stored"  # ex.: "Hot LRS Data Stored"

    filters = {
        "serviceName": "Storage",
        "armRegionName": region,
        "priceType": price_type,
    }

    def select(items: list[dict]) -> dict:
        # Match EXATO de produto/sku/meter — substring pega Page Blob, Managed
        # Disks, Files etc. e o meter de capacidade ("Data Stored") convive com
        # transações/operações no mesmo sku.
        cands = [it for it in items if str(it.get("productName")) == product]
        cands = [it for it in cands if str(it.get("skuName")) == sku]
        cands = [it for it in cands if str(it.get("meterName")) == meter]
        # TODO(heurística): "Data Stored" tem FAIXAS DE VOLUME (tierMinimumUnits
        # 0 / 51200 / 512000 GB). Usamos a faixa base (tierMinimumUnits == 0), que
        # é o preço marginal do primeiro tier. Estimativas de grandes volumes
        # precisariam somar as faixas.
        base = [it for it in cands if not it.get("tierMinimumUnits")]
        if base:
            cands = base
        cands = _primary_only(cands)
        return _select_unique(
            cands, f"Storage {product} {tier}/{redundancy} 'Data Stored' em {region}"
        )

    return filters, select


# --------------------------------------------------------------------------- #
# SQL Database — múltiplos meters; isola por tier + compute + vCores.
# --------------------------------------------------------------------------- #
def resolve_sql(config: dict) -> Resolver:
    region = config["region"]
    tier = config.get("tier", "General Purpose")  # General Purpose/Business Critical...
    compute = config.get("compute", "Provisioned")  # Provisioned/Serverless
    # A família de hardware também vira múltiplos meters (Gen5, DC-Series, FSv2).
    # Default Gen5, que é o padrão da calculadora; sobreponível via config.
    hardware = config.get("hardware", "Gen5")
    vcores = config.get("vCores")
    price_type = config.get("priceType", "Consumption")

    filters = {
        "serviceName": "SQL Database",
        "armRegionName": region,
        "priceType": price_type,
    }

    def select(items: list[dict]) -> dict:
        # productName traz tier + família ("... General Purpose - Compute Gen5").
        cands = [it for it in items if _contains(it, "productName", tier)]
        cands = [it for it in cands if _contains(it, "productName", hardware)]
        # Serverless vs Provisioned aparece em productName ("- Serverless").
        if compute.lower() == "serverless":
            cands = [it for it in cands if _contains(it, "productName", "Serverless")]
        else:
            cands = [it for it in cands if not _contains(it, "productName", "Serverless")]
        # vCores: match EXATO de skuName ("2 vCore"); substring casaria "32/12
        # vCore" e "2 vCore Zone Redundancy".
        if vcores is not None:
            cands = [it for it in cands if str(it.get("skuName")) == f"{vcores} vCore"]
        # Exclui meters gratuitos de dev/test ("... - Free").
        cands = [it for it in cands if not _contains(it, "meterName", "Free")]
        cands = _primary_only(cands)
        return _select_unique(
            cands, f"SQL {tier}/{compute}/{hardware}/{vcores}vCore em {region}"
        )

    return filters, select


# --------------------------------------------------------------------------- #
# Azure Kubernetes Service — SÓ a taxa do control plane gerenciado.
#
# Os NÓS do cluster não entram aqui: são VMs comuns, cobradas por hora como
# qualquer outra, e já resolvidas por resolve_vm (validado no componente
# `node-pool` do padrão aks-microservices). Este resolver cobre exclusivamente a
# taxa fixa do control plane.
# --------------------------------------------------------------------------- #
def resolve_aks(config: dict) -> Resolver:
    region = config["region"]
    # A camada paga do control plane. Sondado em 21/08 (eastus): o único
    # skuName com meter de control plane é "Standard"; o tier gratuito do AKS
    # NÃO tem meter na Retail Prices API (não há o que cobrar), então pedir
    # tier="Free" cai em zero candidatos e levanta PriceResolutionError em vez
    # de devolver um preço inventado.
    tier = config.get("tier", "Standard")
    # "Long Term Support" é um ADICIONAL (suporte estendido de versão do
    # Kubernetes), cobrado à parte do Uptime SLA e 6x mais caro ($0.60/h contra
    # $0.10/h em eastus). Sob o mesmo skuName "Standard" convivem os dois
    # meters, então a escolha tem que ser explícita — sem isso o seletor teria
    # 2 candidatos e, pela regra da casa, não adivinharia.
    long_term_support = bool(config.get("longTermSupport", False))
    price_type = config.get("priceType", "Consumption")

    filters = {
        "serviceName": "Azure Kubernetes Service",
        "armRegionName": region,
        "skuName": tier,
        "priceType": price_type,
    }

    def select(items: list[dict]) -> dict:
        alvo = "Long Term Support" if long_term_support else "Uptime SLA"
        cands = [it for it in items if _contains(it, "meterName", alvo)]
        # ATENÇÃO — NÃO chamar _primary_only aqui. Medido em 21/08 (eastus): o
        # meter certo, "Standard Uptime SLA", vem com isPrimaryMeterRegion=False,
        # enquanto o "Standard Long Term Support" vem com True. Filtrar região
        # primária antes de escolher o meter descartaria exatamente o item
        # procurado e devolveria o adicional de LTS — 6x o preço, em silêncio.
        # É a mesma armadilha já registrada na Fase 1 para o on-demand de VM.
        # _select_unique já deduplica linhas repetidas do mesmo meterId.
        return _select_unique(cands, f"AKS {tier} {alvo} em {region}")

    return filters, select


# --------------------------------------------------------------------------- #
# Azure Synapse Analytics — serverless SQL pool, cobrado por TB PROCESSADO.
#
# Primeiro serviço do projeto que não é cobrado por tempo nem por capacidade
# armazenada: o custo acompanha o volume CONSULTADO. Por isso exigiu um
# mapeamento de unidade novo ("1 TB") em pricing.monthly_cost.
# --------------------------------------------------------------------------- #

# tier -> productName exato na API. Allowlist de propósito: o serviceName
# "Azure Synapse Analytics" cobre um catálogo enorme e heterogêneo (Dedicated
# SQL Pool por DWU/hora, Spark Pool por vCore/hora, Pipelines por operação,
# Storage por GB/mês, e dezenas de VMs de SSIS), e cada um desses eixos teria
# config e unidade próprias. Um tier fora desta lista para com erro claro em
# vez de tentar casar por substring e cair num meter de outro eixo.
# Chave = grafia canônica aceita em config['tier'] (a comparação é
# case-insensitive); valor = productName exato na API. Público porque o
# catalog.py o consome para montar o schema — fonte única da verdade sobre
# quais motores do Synapse têm resolver.
SYNAPSE_TIERS = {
    "Serverless SQL Pool": "Azure Synapse Analytics Serverless SQL Pool",
}
_SYNAPSE_TIER_LOOKUP = {k.lower(): v for k, v in SYNAPSE_TIERS.items()}


def resolve_synapse(config: dict) -> Resolver:
    region = config["region"]
    tier = str(config.get("tier", "Serverless SQL Pool")).strip()
    price_type = config.get("priceType", "Consumption")

    product = _SYNAPSE_TIER_LOOKUP.get(tier.lower())
    if product is None:
        # Falha ANTES da rede: é config inválida, não ausência de meter.
        raise PriceResolutionError(
            f"tier {tier!r} não é suportado pelo resolver de Synapse "
            f"(suportados: {sorted(SYNAPSE_TIERS)}). Dedicated SQL "
            "Pool, Spark Pool, Pipelines e SSIS são cobrados por outros eixos "
            "e ainda não têm resolver."
        )

    filters = {
        "serviceName": "Azure Synapse Analytics",
        "armRegionName": region,
        "priceType": price_type,
    }

    def select(items: list[dict]) -> dict:
        # Match EXATO de productName. Substring de "Serverless" pegaria também
        # o "Serverless Apache Spark Pool", que é outro produto e é cobrado por
        # vCore/HORA — casaria por engano e devolveria preço de outra unidade.
        cands = [it for it in items if str(it.get("productName")) == product]
        # Dentro do produto, o meter de consulta é o "Data Processed".
        cands = [it for it in cands if _contains(it, "meterName", "Data Processed")]
        cands = _primary_only(cands)
        return _select_unique(cands, f"Synapse {tier} 'Data Processed' em {region}")

    return filters, select


# --------------------------------------------------------------------------- #
# Licença do SQL Database — cobrada À PARTE do compute, e SEM região.
#
# O meter que resolve_sql escolhe (produto "... General Purpose - Compute
# Gen5") é a linha de COMPUTE, sem licença. Ela bate no centavo com a linha
# "Compute" da calculadora, mas o item completo lá custa ~66% mais, porque soma
# a licença do SQL Server. Até 21/08 isso fazia estimate_monthly_cost
# SUBESTIMAR qualquer banco com licença inclusa.
#
# ONDE ELA ESTAVA ESCONDIDA — duas razões, e as duas já eram armadilhas
# conhecidas do projeto:
#
#   1. armRegionName = "Global". A licença NÃO tem região comercial: é a mesma
#      no mundo todo (fora US Gov). Procurar em "eastus" devolvia zero e nada
#      indicava que havia mais o que procurar — exatamente o modo de falha que
#      _SPECIAL_REGIONS existe para evitar (ver retail_client.py).
#   2. "License" aparece no productName, não no meterName. O meterName é só
#      "vCore", então procurar por meterName ~ "License" também dava zero.
#
# Confirmado ao vivo em 21/08: 0.099966/h por vCore x 2 vCores x 730h =
# $145.95 — o valor exato da linha "License" da calculadora.
# --------------------------------------------------------------------------- #

# tier -> productName EXATO da linha de licença.
SQL_LICENSE_PRODUCTS = {
    "General Purpose": (
        "SQL Database Single/Elastic Pool General Purpose - SQL License"
    ),
    "Business Critical": (
        "SQL Database Single/Elastic Pool Business Critical - SQL License"
    ),
    "Hyperscale": "SQL Database SingleDB/Elastic Pool Hyperscale - SQL License",
}
_SQL_LICENSE_LOOKUP = {k.lower(): v for k, v in SQL_LICENSE_PRODUCTS.items()}

# A licença é global, com UMA exceção: as regiões Gov têm meter próprio (mais
# caro — $0.124957/h contra $0.099966/h em GP). Sondado em 21/08.
_SQL_LICENSE_REGION = "Global"
_SQL_LICENSE_GOV_REGION = "US Gov"


def resolve_sql_license(config: dict) -> Resolver:
    """Resolve a linha de LICENÇA do SQL Database (preço por vCore/hora).

    ATENÇÃO À UNIDADE: o preço é por **vCore**-hora, não pelo banco inteiro —
    diferente do meter de compute, cujo skuName já embute a contagem ("2
    vCore"). Multiplicar pelo número de vCores é responsabilidade de quem
    projeta o custo; `pricing.sql_monthly_cost` faz isso a partir do
    config['vCores'].
    """
    tier = str(config.get("tier", "General Purpose")).strip()
    region = str(config.get("region", ""))
    price_type = config.get("priceType", "Consumption")

    product = _SQL_LICENSE_LOOKUP.get(tier.lower())
    if product is None:
        raise PriceResolutionError(
            f"tier {tier!r} não tem linha de licença mapeada "
            f"(conhecidos: {sorted(SQL_LICENSE_PRODUCTS)})."
        )

    # Gov tem meter próprio; o resto do mundo compartilha o "Global".
    slug = region.lower().replace(" ", "")
    license_region = (
        _SQL_LICENSE_GOV_REGION
        if slug.startswith(("usgov", "usdod"))
        else _SQL_LICENSE_REGION
    )

    filters = {
        "serviceName": "SQL Database",
        "armRegionName": license_region,
        "priceType": price_type,
    }

    def select(items: list[dict]) -> dict:
        cands = [it for it in items if str(it.get("productName")) == product]
        # O filtro de priceType já derruba a linha DevTestConsumption, que vem
        # com o MESMO meterId e preço 0.00 — se ela passasse, _dedup NÃO a
        # colapsaria (a chave inclui o preço) e sobrariam 2 candidatos.
        cands = [it for it in cands if str(it.get("type")) == str(price_type)]
        return _select_unique(cands, f"Licença SQL {tier} ({license_region})")

    return filters, select


# Registro serviço -> resolver, usado por pricing.resolve_price.
RESOLVERS: dict[str, Callable[[dict], Resolver]] = {
    "vm": resolve_vm,
    "storage": resolve_storage,
    "sql": resolve_sql,
    "aks": resolve_aks,
    "synapse": resolve_synapse,
    "sql_license": resolve_sql_license,
}
