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


# Pseudo-regiões: valores de armRegionName que NÃO são slugs de região comercial.
# A API os devolve com capitalização e espaços significativos ("Global", "US Gov",
# "Zone 1") e o $filter é case-sensitive: `armRegionName eq 'global'` casa ZERO
# itens e volta vazio, sem erro. Como o lower+replace do fallback genérico produz
# exatamente essa forma, todo serviço sem região comercial (Load Balancer,
# bandwidth/egress, DNS, CDN, Traffic Manager...) sumia silenciosamente da
# resolução e da enumeração do catálogo — o pior modo de falha do projeto.
#
# Por isso essas regiões são exceção: a allowlist abaixo é consultada ANTES do
# lower+replace e devolve a grafia canônica que a API aceita. A chave é a forma
# normalizada (minúscula, sem espaços), então tanto "Global" quanto "global"
# quanto "GLOBAL" chegam ao mesmo canônico.
#
# Grafias confirmadas por sondagem da própria API (distintos de armRegionName),
# não por suposição. Nota: NÃO existe "US DoD" como armRegionName — as regiões
# DoD/Gov reais vêm como slug comum ("usdodeast", "usgovvirginia", "usgovtexas")
# e continuam sendo tratadas pelo caminho comercial.
_SPECIAL_REGIONS = (
    "Global",
    "US Gov",
    "US Gov Zone 1",
    "US Gov Zone 2",
    "Zone 1",
    "Zone 2",
    "Zone 3",
    "Zone 4",
    "Zone 5",
    "Zone 6",
    "Intercontinental",
    # Pseudo-regiões continentais usadas por meters de banda/egress.
    "Asia",
    "Europe",
    "India",
    "Middle East And Africa",
    "North America",
    "Oceania",
    "South America",
)

# forma normalizada -> grafia canônica esperada pela API.
_SPECIAL_REGION_MAP = {r.lower().replace(" ", ""): r for r in _SPECIAL_REGIONS}


def normalize_region(region: str) -> str:
    """Normaliza um nome de região para o formato armRegionName.

    "East US" -> "eastus". Aceita tanto o nome amigável quanto o já-normalizado.

    Pseudo-regiões não-comerciais ("Global", "US Gov", "Zone 1"...) são
    preservadas na capitalização exata que o $filter da API exige — ver
    _SPECIAL_REGIONS. Sem isso a consulta casaria zero itens em silêncio.
    """
    if not region:
        return region
    key = region.strip().lower()
    # Antes de qualquer slugify: pseudo-regiões mantêm a grafia canônica.
    special = _SPECIAL_REGION_MAP.get(key.replace(" ", ""))
    if special is not None:
        return special
    if key in _REGION_MAP:
        return _REGION_MAP[key]
    # Fallback genérico: remove espaços (cobre regiões fora do mapa mínimo).
    return key.replace(" ", "")


def build_filter(filters: dict[str, str]) -> str:
    """Monta uma expressão OData $filter a partir de um dict campo->valor.

    Cada par vira `campo eq 'valor'`, unidos por ` and `. Aspas simples no valor
    são escapadas dobrando-as, como manda o OData. A região, se presente, é
    normalizada aqui: regiões comerciais viram slug minúsculo sem espaços
    ("East US" -> "eastus"), pseudo-regiões mantêm a grafia canônica da API
    ("Global", "US Gov") — ver normalize_region.
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
        self, filters: dict[str, str], currency: str = "USD", top: int | None = None
    ) -> list[dict]:
        """Consulta todos os itens que casam com `filters`, com paginação completa.

        A paginação é dirigida SEMPRE por $skip, derivado do total de itens já
        acumulados — nunca por NextPageLink. O link é conhecido por voltar vazio
        de forma intermitente; usá-lo como controle de fluxo criava dois modos de
        avanço (link e $skip) que dessincronizavam entre si e refaziam páginas.
        Com uma fonte única de verdade (`len(items)`), a mistura deixa de existir.

        Condição de parada: página com menos de PAGE_SIZE itens.

        `top` limita a consulta a N itens (sondas baratas de descoberta): manda
        $top para a API e trunca localmente, então o limite vale mesmo quando a
        API ignora o parâmetro.
        """
        odata_filter = build_filter(filters)
        base_params = {
            "api-version": API_VERSION,
            "currencyCode": currency,
            "$filter": odata_filter,
        }
        if top is not None:
            base_params["$top"] = str(min(top, PAGE_SIZE))

        items: list[dict] = []
        while True:
            params = dict(base_params)
            if items:
                # A primeira chamada vai sem $skip; as seguintes retomam
                # exatamente de onde a anterior parou.
                params["$skip"] = str(len(items))

            data = await self._get(BASE_URL, params)
            page = data.get("Items", [])
            items.extend(page)

            if top is not None and len(items) >= top:
                return items[:top]
            if len(page) < PAGE_SIZE:
                return items
