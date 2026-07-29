"""Modelos de dados (pydantic v2) compartilhados pelo MCP."""

from __future__ import annotations

from pydantic import BaseModel


class PriceResult(BaseModel):
    """Preço unitário resolvido para um único meter da Azure Retail Prices API."""

    unit_price: float
    currency: str
    unit_of_measure: str
    meter_id: str
    meter_name: str
    sku_name: str
    region: str
    price_type: str
