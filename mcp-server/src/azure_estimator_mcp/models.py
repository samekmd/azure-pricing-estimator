"""Modelos de dados (pydantic v2) compartilhados pelo MCP."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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


class AddLineItemResult(BaseModel):
    """O que add_line_item conseguiu (ou não) aplicar na UI da calculadora.

    Só é interessante no modo não-estrito: no estrito, um campo sem seletor
    levanta NotImplementedError e nunca chega a virar resultado parcial.
    """

    service: str
    applied_fields: list[str]
    ignored_fields: list[str] = Field(default_factory=list)
    """Campos sem seletor mapeado, pulados por strict=False (vazio no estrito).

    Não-vazio significa que a estimativa na calculadora NÃO reflete toda a
    config pedida — quem exporta o link precisa dizer isso ao usuário.
    """

    @property
    def complete(self) -> bool:
        return not self.ignored_fields


class ServiceMatch(BaseModel):
    """Um serviço Azure candidato devolvido por search_azure_services."""

    key: str
    """Chave do resolver em meters.RESOLVERS — é o que resolve_price espera."""

    service_name: str
    """serviceName EXATO como a Retail Prices API o devolve (usado no $filter)."""

    label: str
    service_family: str | None = None
    matched_on: str
    """Termo do catálogo que casou com a busca — ajuda o agente a entender o hit."""


class FieldSchema(BaseModel):
    """Um campo de config esperado pelo resolver, com seus valores válidos."""

    name: str
    type: str
    required: bool
    description: str
    default: Any | None = None
    values: list[Any] = Field(default_factory=list)
    """Amostra dos valores válidos — pode ser um recorte, ver values_truncated."""

    value_count: int | None = None
    """Total de valores distintos encontrados (None = não enumerável)."""

    values_truncated: bool = False
    values_source: str | None = None
    """Consulta da Retail Prices API de onde os valores saíram (para obter todos)."""


class ServiceConfigSchema(BaseModel):
    """Schema de configuração de um serviço, devolvido por get_service_config_schema."""

    key: str
    service_name: str
    region: str
    currency: str
    fields: list[FieldSchema]
    example_config: dict[str, Any]
    """Config pronta para ser passada a resolve_price(key, config)."""

    notes: list[str] = Field(default_factory=list)
