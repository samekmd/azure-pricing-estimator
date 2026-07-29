"""Cliente assíncrono da Azure Retail Prices API (pública, sem autenticação).

Endpoint: https://prices.azure.com/api/retail/prices
Docs: https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices

Este módulo é HTTP puro (httpx) e NÃO depende de navegador/Playwright — deve
permanecer totalmente desacoplado de calculator_client.py.
"""

from __future__ import annotations

import asyncio

import httpx

BASE_URL = "https://prices.azure.com/api/retail/prices"
# api-version com savings plan nos itens (exigido pelas instruções da Fase 1).
API_VERSION = "2023-01-01-preview"
PAGE_SIZE = 1000  # máximo de itens por página retornado pela API

# Mapa mínimo de normalização de região. Fora daqui, caímos no _slugify.
_REGION_MAP = {
    "east us": "eastus",
    "east us 2": "eastus2",
    "west us": "westus",
    "west us 2": "westus2",
    "central us": "centralus",
    "west europe": "westeurope",
    "north europe": "northeurope",
    "uk south": "uksouth",
    "southeast asia": "southeastasia",
    "brazil south": "brazilsouth",
}


def normalize_region(region: str) -> str:
    """Normaliza um nome de região para o formato armRegionName.

    "East US" -> "eastus". Aceita tanto o nome amigável quanto o já-normalizado.
    """
    if not region:
        return region
    key = region.strip().lower()
    if key in _REGION_MAP:
        return _REGION_MAP[key]
    # Fallback genérico: remove espaços (cobre regiões fora do mapa mínimo).
    return key.replace(" ", "")


def build_filter(filters: dict[str, str]) -> str:
    """Monta uma expressão OData $filter a partir de um dict campo->valor.

    Cada par vira `campo eq 'valor'`, unidos por ` and `. Aspas simples no valor
    são escapadas dobrando-as, como manda o OData. A região, se presente, é
    normalizada aqui (armRegionName é minúsculo e sem espaços).
    """
    parts: list[str] = []
    for field, value in filters.items():
        if value is None:
            continue
        if field == "armRegionName":
            value = normalize_region(str(value))
        escaped = str(value).replace("'", "''")
        parts.append(f"{field} eq '{escaped}'")
    return " and ".join(parts)


class RetailPricesClient:
    """Cliente async para consultar preços de varejo do Azure com paginação."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        # Se um client externo for injetado (testes), não o fechamos.
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "RetailPricesClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def _get(self, url: str, params: dict[str, str] | None) -> dict:
        """GET com retry/backoff exponencial em 429 e 5xx."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.get(url, params=params, timeout=self._timeout)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                else:
                    resp.raise_for_status()
                    return resp.json()

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_base * (2**attempt))

        assert last_exc is not None
        raise last_exc

    async def query_prices(
        self, filters: dict[str, str], currency: str = "USD"
    ) -> list[dict]:
        """Consulta todos os itens que casam com `filters`, com paginação completa.

        Segue NextPageLink quando presente; MAS há um bug conhecido em que o link
        volta vazio. Contorno: se a página veio cheia (PAGE_SIZE itens), geramos a
        próxima nós mesmos com $skip incrementado, até vir menos que PAGE_SIZE.
        """
        odata_filter = build_filter(filters)
        params = {
            "api-version": API_VERSION,
            "currencyCode": currency,
            "$filter": odata_filter,
        }

        items: list[dict] = []
        skip = 0
        url: str | None = BASE_URL
        # Na primeira chamada usamos params; ao seguir NextPageLink, a URL já
        # traz a query string, então params vira None.
        current_params: dict[str, str] | None = params

        while url is not None:
            data = await self._get(url, current_params)
            page = data.get("Items", [])
            items.extend(page)

            next_link = data.get("NextPageLink")
            if next_link:
                url = next_link
                current_params = None
                continue

            # Sem NextPageLink: só continuamos se a página veio cheia (contorno
            # do bug). Página incompleta => acabou de verdade.
            if len(page) == PAGE_SIZE:
                skip += PAGE_SIZE
                url = BASE_URL
                current_params = {**params, "$skip": str(skip)}
                continue

            url = None

        return items
