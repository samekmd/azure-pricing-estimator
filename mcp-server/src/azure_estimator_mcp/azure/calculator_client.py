"""Cliente assíncrono da calculadora de preços da Azure, dirigido por Playwright.

Reutiliza o estado de sessão salvo pelo scripts/bootstrap_login.py para dirigir
a UI da calculadora já autenticado — a autenticação é delegada a um navegador
real (o jeito que a Microsoft desenhou: cookie + CSRF de uma sessão de verdade).

Os seletores usados abaixo foram mapeados ao vivo (DOM real, sessão
autenticada) em 06/08. Preferência de estabilidade: `data-testid` > `name`/
`id` de campo > classe Fluent UI (hash, evitada sempre que possível).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

CALCULATOR_URL = "https://azure.microsoft.com/en-us/pricing/calculator/"

# Serviço (chave de RESOLVERS em meters.py / _CATALOG em catalog.py) -> testid
# do botão "Add to estimate" no product picker. Cada testid aparece 2x no DOM
# (aba "Popular" + aba da categoria própria) — só uma cópia fica visível por
# vez, por isso o seletor sempre filtra `:visible`.
_PICKER_TESTIDS: dict[str, str] = {
    "vm": "virtual-machines-picker-item",
    "storage": "storage-picker-item",
    "sql": "azure-sql-database-picker-item",
}

# Campos de config que são <select name="..."> simples dentro do painel do
# item já adicionado — mapeáveis direto por select_option(label=...). Campos
# fora dessa lista (busca de instância/tamanho, quantidade, radios de
# billing) usam widgets custom (combobox com busca, grupos de radio cujo
# `name` embute um GUID por item) que ainda não foram mapeados; ver
# add_line_item().
_SIMPLE_SELECT_FIELDS: dict[str, set[str]] = {
    "vm": {"region", "operatingSystem", "type", "tier", "category"},
    "storage": {
        "region",
        "type",
        "performanceTier",
        "storageAccountType",
        "fileStructure",
        "accessTier",
        "redundancy",
    },
    "sql": {
        "region",
        "type",
        "purchaseModel",
        "vcoreTier",
        "computeTier",
        "generation",
        "instanceSize",
        "zoneRedundancy",
    },
}


class AzureCalculatorClient:
    """Context manager async que abre a calculadora com uma sessão já logada.

    A auth NÃO é renovada automaticamente: se a sessão expirou, is_authenticated
    retorna False e cabe a você rodar o bootstrap_login.py de novo. O ponto de
    usar navegador é justamente delegar a autenticação a ele.
    """

    def __init__(
        self,
        storage_state_path: str = ".auth/storage_state.json",
        headless: bool = True,
    ) -> None:
        self.storage_state_path = Path(storage_state_path)
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "AzureCalculatorClient":
        if not self.storage_state_path.exists():
            raise FileNotFoundError(
                f"Estado de sessão não encontrado em '{self.storage_state_path}'. "
                "Rode primeiro: uv run python mcp-server/scripts/bootstrap_login.py"
            )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            storage_state=str(self.storage_state_path)
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Fecha na ordem inversa da criação, tolerando componentes não iniciados.
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def is_authenticated(self) -> bool:
        """Reporta se a sessão carregada está logada NA CALCULADORA.

        A calculadora (SPA) tem seu PRÓPRIO widget de conta, independente do
        header global da Microsoft — que continua mostrando "Sign in" mesmo com
        você autenticado. Por isso o sinal confiável é o botão de conta da
        calculadora (#user-display, dentro de .calc-login), que só aparece
        quando a sessão está válida. Seletor calibrado contra a página real.

        TODO: o DOM do estado DESLOGADO não foi capturado (exigiria invalidar a
        sessão); se o layout da calculadora mudar, revalidar este seletor.
        """
        if self._context is None:
            raise RuntimeError(
                "Cliente não inicializado. Use como context manager async "
                "(async with AzureCalculatorClient() as client: ...)."
            )

        page = await self._context.new_page()
        try:
            await page.goto(CALCULATOR_URL, wait_until="networkidle")
            user_display = page.locator("#user-display")
            try:
                await user_display.wait_for(state="visible", timeout=8000)
                return True
            except PlaywrightTimeoutError:
                # Widget de conta da calculadora não apareceu -> sessão inválida.
                return False
        finally:
            await page.close()

    async def create_estimate(self) -> None:
        """Abre uma aba nova na calculadora com uma estimativa vazia.

        Precisa ser chamado antes de add_line_item()/export_estimate() — eles
        reutilizam a página aberta aqui (guardada em self._page).
        """
        if self._context is None:
            raise RuntimeError(
                "Cliente não inicializado. Use como context manager async "
                "(async with AzureCalculatorClient() as client: ...)."
            )

        self._page = await self._context.new_page()
        await self._page.goto(CALCULATOR_URL, wait_until="networkidle")

        # Banner de cookies (opcional — só aparece na primeira visita da
        # sessão de navegador). Rejeita não-essenciais por padrão.
        try:
            await self._page.get_by_role("button", name="Reject").click(timeout=3000)
        except PlaywrightTimeoutError:
            pass

    async def add_line_item(self, service: str, config: dict[str, Any]) -> None:
        """Adiciona um serviço à estimativa e aplica os campos simples de config.

        `service` usa as mesmas chaves de RESOLVERS (meters.py) / _CATALOG
        (catalog.py): "vm", "storage", "sql". `config` só aceita, por ora, os
        campos listados em _SIMPLE_SELECT_FIELDS (selects nativos do painel);
        os demais — busca de instância/tamanho, quantidade, radios de billing
        (name embute um GUID por item, não é um seletor estável) — ainda não
        têm seletor mapeado, então um campo desconhecido levanta
        NotImplementedError em vez de ser ignorado silenciosamente.
        """
        if self._page is None:
            raise RuntimeError("Chame create_estimate() antes de add_line_item().")

        testid = _PICKER_TESTIDS.get(service)
        if testid is None:
            raise ValueError(
                f"Serviço '{service}' sem seletor mapeado "
                f"(esperado um de {sorted(_PICKER_TESTIDS)})."
            )

        unknown_fields = set(config) - _SIMPLE_SELECT_FIELDS.get(service, set())
        if unknown_fields:
            raise NotImplementedError(
                f"Campo(s) {sorted(unknown_fields)} de '{service}' ainda não "
                "têm seletor mapeado (instância/tamanho, quantidade e radios "
                "de billing usam widgets custom — follow-up)."
            )

        # O testid aparece 2x no DOM (aba "Popular" + aba da categoria); só a
        # cópia visível é clicável.
        add_button = self._page.locator(f'[data-testid="{testid}"]:visible').first
        await add_button.click()
        # Painel de config do item recém-adicionado leva um instante pra montar.
        await self._page.wait_for_timeout(500)

        for field, value in config.items():
            select = self._page.locator(f'select[name="{field}"]').last
            await select.select_option(label=value)

    async def export_estimate(self) -> str:
        """Clica em Compartilhar e devolve o link gerado pela calculadora.

        Fluxo real (menu "..." > Share): abre um dialog (`.share-modal`) com
        o link num `<textarea readonly name="link">` — não é um `<input>` nem
        um `<a href>`, então o valor sai por `input_value()`. Requer sessão
        autenticada (Save/Share ficam desabilitados, com "Log in to Share",
        quando deslogado — is_authenticated() já cobre essa checagem).
        """
        if self._page is None:
            raise RuntimeError("Chame create_estimate() antes de export_estimate().")

        page = self._page
        await page.locator('[data-testid="stickyCostHeader__moreMenuButton"]').click()
        await page.locator('[data-testid="moreOptionsMenuList__share"]').click()

        dialog = page.locator(".share-modal[role=\"dialog\"]")
        link_field = dialog.locator('textarea[name="link"]')
        await link_field.wait_for(state="visible", timeout=10000)
        link = await link_field.input_value()

        await dialog.get_by_role("button", name="Done").click()
        return link
