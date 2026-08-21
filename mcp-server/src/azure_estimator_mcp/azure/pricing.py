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
