"""Cliente assíncrono da calculadora de preços da Azure, dirigido por Playwright.

Reutiliza o estado de sessão salvo pelo scripts/bootstrap_login.py para dirigir
a UI da calculadora já autenticado — a autenticação é delegada a um navegador
real (o jeito que a Microsoft desenhou: cookie + CSRF de uma sessão de verdade).

Este módulo contém apenas a BASE de autenticação. A montagem da estimativa e a
captura do link de compartilhamento são da Fase 2 (stubs abaixo).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

CALCULATOR_URL = "https://azure.microsoft.com/en-us/pricing/calculator/"


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
        raise NotImplementedError(
            "Fase 2: criar uma nova estimativa dirigindo a UI da calculadora."
        )

    async def add_line_item(self, service: str, config: dict[str, Any]) -> None:
        raise NotImplementedError(
            "Fase 2: adicionar um serviço à estimativa dirigindo a UI "
            "(mapear seletores de busca/adição de serviço)."
        )

    async def export_estimate(self) -> str:
        raise NotImplementedError(
            "Fase 2: clicar em compartilhar e capturar o link da estimativa."
        )
