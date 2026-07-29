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


# Registro serviço -> resolver, usado por pricing.resolve_price.
RESOLVERS: dict[str, Callable[[dict], Resolver]] = {
    "vm": resolve_vm,
    "storage": resolve_storage,
    "sql": resolve_sql,
}
