"""Orquestração de preço: resolve_price (config -> PriceResult) e monthly_cost.

Amarra os resolvers de meters.py ao RetailPricesClient e converte o item
escolhido em um PriceResult tipado. monthly_cost projeta o custo mensal
respeitando o unit_of_measure do meter.
"""

from __future__ import annotations

import re

from ..models import PriceResult
from .meters import RESOLVERS, PriceResolutionError
from .retail_client import RetailPricesClient, normalize_region

DEFAULT_MONTHLY_HOURS = 730  # padrão da própria calculadora Azure
# Convenção adotada para unidades diárias ("1/Day"): 30 dias por mês, fixo.
# Não é 365/12 (30.4375) por decisão deliberada — 30 foi o fator que casou com a
# calculadora oficial da Azure (validado no ACR Premium: preço diário x30 bate
# com os ~US$ 50/mês anunciados lá). Manter 30 mantém a estimativa alinhada ao
# número que o usuário vê na calculadora.
DAYS_PER_MONTH = 30


async def resolve_price(
    service: str,
    config: dict,
    currency: str = "USD",
    client: RetailPricesClient | None = None,
) -> PriceResult:
    """Resolve o preço unitário de um serviço a partir da sua config.

    Escolhe o resolver do serviço, consulta a Retail Prices API, aplica a
    seleção do item e devolve um PriceResult. Levanta PriceResolutionError se o
    resolver não isolar exatamente um meter.
    """
    try:
        resolver = RESOLVERS[service]
    except KeyError:
        raise PriceResolutionError(
            f"Serviço desconhecido: {service!r}. Conhecidos: {sorted(RESOLVERS)}"
        )

    filters, select = resolver(config)

    owns_client = client is None
    client = client or RetailPricesClient()
    try:
        items = await client.query_prices(filters, currency=currency)
    finally:
        if owns_client:
            await client.aclose()

    item = select(items)

    return PriceResult(
        unit_price=item["retailPrice"],
        currency=item.get("currencyCode", currency),
        unit_of_measure=item["unitOfMeasure"],
        meter_id=item["meterId"],
        meter_name=item["meterName"],
        sku_name=item.get("skuName", ""),
        region=normalize_region(config.get("region", "")),
        price_type=item.get("type") or item.get("priceType", ""),
    )


def monthly_cost(price: PriceResult, usage: dict) -> float:
    """Custo mensal respeitando o unit_of_measure do meter.

    Mapeamento de unidades suportadas:
      "1 Hour"     -> usage['hours'] (default 730h/mês)
      "1 GB/Month" -> usage['gb']
      "1 GB"       -> usage['gb']
      "1 TB"       -> usage['tbProcessed'] (ou usage['tb'])
      "1/Month"    -> 1 (custo fixo mensal)
      "1 Month"    -> 1
      "1/Day"      -> DAYS_PER_MONTH (custo diário projetado ao mês)
      "1 Day"      -> idem
    Unidade não reconhecida levanta ValueError em vez de chutar.
    """
    uom = price.unit_of_measure.strip()
    # Separa o prefixo numérico da unidade. O separador é inconsistente na API:
    # "1 Hour" e "100 GB/Month" têm espaço, mas "1/Month" não tem.
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(.*)$", uom)
    if m:
        factor = float(m.group(1))
        unit = m.group(2).strip().lower()
    else:
        factor = 1.0
        unit = uom.lower()

    if unit == "hour":
        quantity = usage.get("hours", DEFAULT_MONTHLY_HOURS)
    elif unit in ("gb/month", "gb"):
        quantity = usage.get("gb")
        if quantity is None:
            raise ValueError(
                f"usage['gb'] é obrigatório para unit_of_measure {price.unit_of_measure!r}"
            )
    elif unit in ("tb/month", "tb"):
        # Unidade de VOLUME PROCESSADO (Synapse serverless SQL pool: $5/TB
        # consultado). Aceita as duas chaves de propósito: 'tbProcessed' é o
        # nome usado nos padrões da Skill, por ser descritivo de "dados
        # processados"; 'tb' mantém a simetria com 'gb' para quem escrever a
        # config na mão. Sem default: diferente de horas (onde 730 é o mês
        # cheio, uma convenção defensável), não existe volume consultado
        # "padrão" — inventar um seria inventar a conta inteira.
        quantity = usage.get("tbProcessed", usage.get("tb"))
        if quantity is None:
            raise ValueError(
                f"usage['tbProcessed'] é obrigatório para unit_of_measure "
                f"{price.unit_of_measure!r} (volume consultado no mês, em TB)"
            )
    elif unit in ("/month", "month"):
        quantity = 1
    elif unit in ("/day", "day"):
        # A unidade já chega normalizada (minúscula, sem espaços nas bordas),
        # então "1/Day", "1 /Day" e "1 Day" caem todas aqui.
        quantity = usage.get("days", DAYS_PER_MONTH)
    else:
        raise ValueError(
            f"unit_of_measure não reconhecido: {price.unit_of_measure!r}. "
            "Adicione o mapeamento em monthly_cost antes de estimar."
        )

    # unit_price é por 'factor' unidades (ex.: "100 GB/Month" -> por 100 GB).
    return price.unit_price * (quantity / factor)


# --------------------------------------------------------------------------- #
# SQL Database: compute + licença.
#
# O meter que resolve_sql escolhe é só a linha de COMPUTE. Somar a licença é o
# que separa "resolve" de "resolve certo": sem ela, um banco com licença
# inclusa era subestimado em ~66%.
# --------------------------------------------------------------------------- #
async def sql_monthly_cost(
    config: dict,
    usage: dict | None = None,
    currency: str = "USD",
    client: RetailPricesClient | None = None,
) -> dict:
    """Custo mensal de um SQL Database, aberto em compute + licença.

    A licença é cobrada por **vCore**-hora e vem de um meter separado, sem
    região ("Global") — por isso ela é multiplicada aqui pelo `vCores` da
    config, e não dentro de monthly_cost, que só enxerga o `usage` e não teria
    como saber a contagem.

    `config['licenseIncluded']` (default `True`) espelha o
    `softwareBillingOption` da calculadora: `False` significa Azure Hybrid
    Benefit (BYOL), em que a licença não é cobrada.

    NÃO inclui armazenamento de dados nem backup — a calculadora os cobra à
    parte e eles dependem de config que o padrão não determina (GB de dados,
    retenção). Em GP/Gen5/2 vCore eram ~1,3% do item contra ~66% da licença.
    """
    usage = usage or {}
    license_included = bool(config.get("licenseIncluded", True))
    vcores = config.get("vCores")

    # Validado ANTES de qualquer chamada: sem isto, a falta de vCores aparecia
    # como o erro de AMBIGUIDADE do resolver de compute ("6 candidatos: '2
    # vCore', '4 vCore'…"), que é verdadeiro mas manda o leitor investigar o
    # meter errado. A causa real é uma config incompleta.
    if license_included and vcores is None:
        raise ValueError(
            "config['vCores'] é obrigatório para calcular a licença do SQL "
            "(ela é cobrada por vCore/hora). Use licenseIncluded=False para "
            "Azure Hybrid Benefit (BYOL)."
        )

    owns_client = client is None
    client = client or RetailPricesClient()
    try:
        compute_price = await resolve_price(
            "sql", config, currency=currency, client=client
        )
        compute = monthly_cost(compute_price, usage)

        license_price = None
        license_cost = 0.0
        if license_included:
            license_price = await resolve_price(
                "sql_license", config, currency=currency, client=client
            )
            license_cost = monthly_cost(license_price, usage) * int(vcores)
    finally:
        if owns_client:
            await client.aclose()

    return {
        "compute": compute,
        "license": license_cost,
        "total": compute + license_cost,
        "license_included": license_included,
        "compute_meter_id": compute_price.meter_id,
        "license_meter_id": license_price.meter_id if license_price else None,
        "currency": compute_price.currency,
    }
