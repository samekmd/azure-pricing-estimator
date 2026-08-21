"""Cliente assíncrono da calculadora de preços da Azure, dirigido por Playwright.

Reutiliza o estado de sessão salvo pelo scripts/bootstrap_login.py para dirigir
a UI da calculadora já autenticado — a autenticação é delegada a um navegador
real (o jeito que a Microsoft desenhou: cookie + CSRF de uma sessão de verdade).

Os seletores usados abaixo foram mapeados ao vivo (DOM real, sessão
autenticada) em 06/08 e os de sessão revalidados em 21/08. Preferência de
estabilidade: `data-testid` > `name`/`id` de campo > classe Fluent UI (hash,
evitada sempre que possível).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from azure_estimator_mcp.models import AddLineItemResult

CALCULATOR_URL = "https://azure.microsoft.com/en-us/pricing/calculator/"

# Os dois sinais de estado de sessão, EXCLUSIVOS entre si. Ambos conferidos ao
# vivo em 21/08, cada um no seu estado (deslogado: contexto novo sem
# storage_state; logado: .auth/storage_state.json recém-bootstrapado):
#
#                        deslogado   logado
#   _SIGN_IN_BUTTON        count 1   count 0
#   _USER_DISPLAY          count 0   count 1
#
# Checar os dois, e não um só, é o que torna a checagem revalidável: com um
# sinal só, se a calculadora renomear aquele nó a resposta trava numa das duas
# (sempre "deslogado" se olhamos só o widget de conta; sempre "logado" se
# olhamos só o botão de login) e nada denuncia. Com os dois, "nenhum apareceu"
# é um estado detectável — vira erro explícito em vez de palpite.
_SIGN_IN_BUTTON = '[data-testid="stickyCostHeader__signInButton"]'
_USER_DISPLAY = "#user-display"  # dentro de .calc-login, o widget de conta da SPA

# Deslogado, Save/Share do menu "..." NÃO ficam desabilitados como se supunha:
# seguem com is_enabled() == True e embutem um <a href="/auth/signin/...">
# rotulado "Log in to Share". Clicar navega pro login em vez de abrir o dialog
# — era daí que vinha o timeout de 10s esperando .share-modal.
_SHARE_LOGIN_LINK = 'a[href*="/auth/signin"]'

# Margem pra hidratação da SPA antes de decidir o estado da sessão.
_AUTH_PROBE_TIMEOUT_MS = 8000


class CalculatorAuthStateUnknownError(RuntimeError):
    """Nenhum dos dois sinais de sessão apareceu — o DOM mudou.

    Separado de CalculatorSessionExpiredError de propósito: refazer o
    bootstrap não conserta seletor obsoleto. Aqui o recado é pra quem mantém o
    módulo (revalidar contra a página real), não pro usuário final.
    """

    def __init__(self) -> None:
        super().__init__(
            "Não foi possível determinar o estado da sessão: nem "
            f"'{_SIGN_IN_BUTTON}' (deslogado) nem '{_USER_DISPLAY}' (logado) "
            "apareceram. Provável mudança no DOM da calculadora — revalidar os "
            "seletores contra a página real."
        )


class CalculatorSessionExpiredError(RuntimeError):
    """A sessão salva em .auth/storage_state.json não está mais autenticada.

    Não há auto-refresh por design (ver CLAUDE.md): a saída é refazer o
    bootstrap manual. A mensagem já diz isso pra falha não chegar ao usuário
    como um timeout genérico do Playwright.
    """

    def __init__(self, detalhe: str = "") -> None:
        msg = (
            "Sessão da calculadora expirada ou inválida. Rode de novo: "
            "make bootstrap-login (ou uv run python "
            "mcp-server/scripts/bootstrap_login.py)."
        )
        super().__init__(f"{msg} {detalhe}".strip())


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

# Campos de texto simples (<input>, sem widget de busca) do painel de VM.
# "count"/"hours" têm name= e id= iguais; select_option cobre a unidade.
_VM_TEXT_INPUT_FIELDS = {"count", "hours"}

# O campo INSTANCE (#size) é um combobox com busca (react-select) — digitar
# filtra e abre um <div role="option"> com o texto completo do SKU (ex.:
# "D4s v3: 4 vCPUs, 16 GB RAM, ..., $0.376/hour"); precisa digitar algo
# específico o bastante pra sobrar 1 opção, senão o `.first` pode escolher
# a sugestão errada.
_VM_SIZE_FIELD = "size"

# Radios de billing: o `name`/`id` real embute um GUID por item
# (radio-<prefixo>-<GUID>-computeBillingOption), então localizamos pelo
# prefixo estável do id, não pelo texto do label (o label de savings plan
# inclui um "~X% discount" que varia por SKU/região).
_VM_COMPUTE_BILLING_PREFIXES = {
    "payg": "radio-payg-",  # Pay as you go
    "savings_1yr": "radio-sv-one-year-",  # 1 year savings plan
    "savings_3yr": "radio-sv-three-year-",  # 3 year savings plan
    "reserved_1yr": "radio-one-year-",  # 1 year reserved
    "reserved_3yr": "radio-three-year-",  # 3 year reserved
}
_VM_OS_BILLING_PREFIXES = {
    "license_included": "radio-payg-",  # License included
    "azure_hybrid_benefit": "radio-ahb-",  # Azure Hybrid Benefit
}


class AzureCalculatorClient:
    """Context manager async que abre a calculadora com uma sessão já logada.

    A auth NÃO é renovada automaticamente: se a sessão expirou,
    is_authenticated retorna False (ou ensure_authenticated levanta
    CalculatorSessionExpiredError) e cabe a você rodar o bootstrap_login.py de
    novo. O ponto de usar navegador é justamente delegar a autenticação a ele.

    Os passos que dirigem a UI passam por _with_retry: a calculadora é uma SPA
    lenta e um timeout isolado costuma ser transitório. Sessão expirada, ao
    contrário, não é retentável — falha na primeira tentativa, com instrução.
    """

    def __init__(
        self,
        storage_state_path: str = ".auth/storage_state.json",
        headless: bool = True,
        max_retries: int = 2,
        backoff_base: float = 0.5,
    ) -> None:
        self.storage_state_path = Path(storage_state_path)
        self.headless = headless
        self._max_retries = max_retries
        self._backoff_base = backoff_base
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

    async def _with_retry(
        self, acao: Callable[[], Awaitable[Any]], descricao: str
    ) -> Any:
        """Executa `acao()` com retry e backoff exponencial em timeout da UI.

        Mesma convenção do retry HTTP de retail_client.py
        (`backoff_base * 2**tentativa`), pelo mesmo motivo: a falha típica aqui
        é transitória (SPA lenta, painel que ainda não montou).

        Só PlaywrightTimeoutError é retentado. CalculatorSessionExpiredError
        não é: repetir o fluxo numa sessão morta só multiplica esperas de 10s e
        atrasa a única mensagem útil. ValueError/NotImplementedError também
        passam direto — retry não conserta seletor ausente nem SKU indisponível.
        """
        last_exc: PlaywrightTimeoutError | None = None
        for tentativa in range(self._max_retries + 1):
            try:
                return await acao()
            except PlaywrightTimeoutError as exc:
                last_exc = exc
                if tentativa < self._max_retries:
                    await asyncio.sleep(self._backoff_base * (2**tentativa))

        assert last_exc is not None
        raise PlaywrightTimeoutError(
            f"{descricao}: falhou após {self._max_retries + 1} tentativas. "
            f"Último erro: {last_exc}"
        )

    async def is_authenticated(self) -> bool:
        """Reporta se a sessão carregada está logada NA CALCULADORA.

        A calculadora (SPA) tem seu PRÓPRIO estado de conta, independente do
        header global da Microsoft — que mostra "Sign in" mesmo com você
        autenticado, e por isso nunca serviu de sinal.

        A checagem é pela PRESENÇA de um dos dois sinais exclusivos descritos
        em _SIGN_IN_BUTTON/_USER_DISPLAY — botão de login => deslogado, widget
        de conta => logado. Nenhum dos dois levanta
        CalculatorAuthStateUnknownError em vez de chutar um dos lados: ausência
        não é evidência aqui, e um palpite calado é exatamente o modo de falha
        que a regra "não adivinhar" (meters.py) existe pra evitar.

        Os dois estados foram confirmados ao vivo em 21/08, cada um contra o
        DOM real — nenhum deles é suposição.
        """
        if self._context is None:
            raise RuntimeError(
                "Cliente não inicializado. Use como context manager async "
                "(async with AzureCalculatorClient() as client: ...)."
            )

        page = await self._context.new_page()
        try:
            await self._with_retry(
                lambda: page.goto(CALCULATOR_URL, wait_until="networkidle"),
                "Carregar a calculadora",
            )
            # A SPA hidrata os dois widgets tarde; espera o primeiro que vier.
            try:
                await page.locator(
                    f"{_SIGN_IN_BUTTON}, {_USER_DISPLAY}"
                ).first.wait_for(state="visible", timeout=_AUTH_PROBE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                raise CalculatorAuthStateUnknownError() from None

            # Ordem importa: na dúvida (os dois presentes num frame de
            # transição), "deslogado" é o palpite seguro — manda refazer o
            # bootstrap em vez de seguir e falhar lá na frente, no export.
            if await page.locator(_SIGN_IN_BUTTON).count() > 0:
                return False
            if await page.locator(_USER_DISPLAY).count() > 0:
                return True
            raise CalculatorAuthStateUnknownError()
        finally:
            await page.close()

    async def ensure_authenticated(self) -> None:
        """Levanta CalculatorSessionExpiredError se a sessão não estiver válida.

        Versão imperativa de is_authenticated(), pra quem só quer seguir o fluxo
        e não tratar um bool. Chame antes de create_estimate() num fluxo longo:
        descobrir a expiração no fim, já com os line items montados, desperdiça
        toda a montagem.
        """
        if not await self.is_authenticated():
            raise CalculatorSessionExpiredError()

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

        page = await self._context.new_page()
        self._page = page
        await self._with_retry(
            lambda: page.goto(CALCULATOR_URL, wait_until="networkidle"),
            "Abrir a calculadora",
        )

        # Banner de cookies (opcional — só aparece na primeira visita da
        # sessão de navegador). Rejeita não-essenciais por padrão.
        try:
            await self._page.get_by_role("button", name="Reject").click(timeout=3000)
        except PlaywrightTimeoutError:
            pass

    async def add_line_item(
        self, service: str, config: dict[str, Any], strict: bool = True
    ) -> AddLineItemResult:
        """Adiciona um serviço à estimativa e aplica os campos de config.

        `service` usa as mesmas chaves de RESOLVERS (meters.py) / _CATALOG
        (catalog.py): "vm", "storage", "sql".

        Campos aceitos em `config`:
        - Os listados em _SIMPLE_SELECT_FIELDS (selects nativos do painel).
        - Só para "vm": _VM_TEXT_INPUT_FIELDS ("count"/"hours", texto puro),
          "hoursFactor" (select de unidade), "size" (combobox de instância
          com busca — ver _apply_vm_size), "computeBillingOption" e
          "osBillingOption" (radios — chave é uma das definidas em
          _VM_COMPUTE_BILLING_PREFIXES/_VM_OS_BILLING_PREFIXES, ex.:
          "reserved_1yr", "azure_hybrid_benefit").

        Para storage/sql, os campos além de _SIMPLE_SELECT_FIELDS (busca de
        instância, quantidade, radios de blob/database billing) ainda não têm
        seletor mapeado.

        `strict=True` (padrão) levanta NotImplementedError nesses campos, em vez
        de ignorá-los calado — mesma regra dos resolvers em meters.py: não
        adivinhar. `strict=False` é a saída explícita pra quem prefere uma
        estimativa parcial a nenhuma: aplica o que tem seletor e devolve os
        pulados em AddLineItemResult.ignored_fields.

        A escolha é de quem chama porque o custo do erro muda com o campo: pular
        "region" ou "capacity" muda o preço do link exportado sem avisar. Ou
        seja, com strict=False, `ignored_fields` não-vazio precisa chegar ao
        usuário junto com o link — o parcial é útil, o parcial silencioso não.

        Observação: com múltiplos itens do mesmo serviço na estimativa, os
        seletores usam a última cópia do campo no DOM (`.last`) — ainda não
        há como mirar um item específico por índice/id.
        """
        if self._page is None:
            raise RuntimeError("Chame create_estimate() antes de add_line_item().")

        testid = _PICKER_TESTIDS.get(service)
        if testid is None:
            raise ValueError(
                f"Serviço '{service}' sem seletor mapeado "
                f"(esperado um de {sorted(_PICKER_TESTIDS)})."
            )

        select_fields = _SIMPLE_SELECT_FIELDS.get(service, set())
        extra_fields: set[str] = set()
        if service == "vm":
            extra_fields = _VM_TEXT_INPUT_FIELDS | {
                "hoursFactor",
                _VM_SIZE_FIELD,
                "computeBillingOption",
                "osBillingOption",
            }

        unknown_fields = set(config) - select_fields - extra_fields
        if unknown_fields and strict:
            raise NotImplementedError(
                f"Campo(s) {sorted(unknown_fields)} de '{service}' ainda não "
                "têm seletor mapeado (instância/quantidade/billing de "
                "storage e sql, por exemplo, são follow-up). "
                "Use strict=False para aplicar o resto assim mesmo."
            )

        # O testid aparece 2x no DOM (aba "Popular" + aba da categoria); só a
        # cópia visível é clicável.
        add_button = self._page.locator(f'[data-testid="{testid}"]:visible').first
        await add_button.click()
        # Painel de config do item recém-adicionado leva um instante pra montar.
        await self._page.wait_for_timeout(500)

        for field, value in config.items():
            if field in unknown_fields:
                continue  # só chega aqui com strict=False; vai em ignored_fields
            if field in select_fields:
                select = self._page.locator(f'select[name="{field}"]').last
                await select.select_option(label=value)
            elif field in _VM_TEXT_INPUT_FIELDS:
                await self._page.locator(f'input[name="{field}"]').last.fill(str(value))
            elif field == "hoursFactor":
                await self._page.locator('select[name="hoursFactor"]').last.select_option(
                    label=value
                )
            elif field == _VM_SIZE_FIELD:
                await self._apply_vm_size(value)
            elif field == "computeBillingOption":
                await self._click_billing_radio(_VM_COMPUTE_BILLING_PREFIXES, value, "computeBillingOption")
            elif field == "osBillingOption":
                await self._click_billing_radio(_VM_OS_BILLING_PREFIXES, value, "osBillingOption")

        return AddLineItemResult(
            service=service,
            applied_fields=sorted(set(config) - unknown_fields),
            ignored_fields=sorted(unknown_fields),
        )

    async def _apply_vm_size(self, search_text: str) -> None:
        """Digita no combobox INSTANCE (#size) e clica a primeira sugestão.

        Widget é um react-select: digitar dispara uma busca assíncrona que
        renderiza <div role="option"> com o SKU completo (ex. "D4s v3: 4
        vCPUs, ..."). `search_text` deve ser específico o bastante pra
        deixar uma opção só (ex. o skuName, não só a série), senão o
        `.first` pode acabar escolhendo o SKU errado.
        """
        assert self._page is not None
        size_input = self._page.locator("#size").last
        await size_input.click()
        await size_input.fill(search_text)
        option = self._page.locator('[role="option"]:visible').first
        await option.wait_for(state="visible", timeout=10000)
        await option.click()

    async def _click_billing_radio(
        self, prefixes: dict[str, str], key: str, group_suffix: str
    ) -> None:
        """Clica o radio de billing correspondente a `key`.

        Algumas opções (ex.: "1 year reserved") ficam desabilitadas pela
        própria calculadora dependendo do SKU/região escolhido ("1 year
        reserved option is not available for your instance selection" — visto
        ao vivo com D4s v3). Checa is_enabled() antes de clicar pra falhar
        rápido e com uma mensagem clara, em vez de esperar o timeout padrão
        do Playwright tentando clicar num elemento desabilitado.
        """
        assert self._page is not None
        prefix = prefixes.get(key)
        if prefix is None:
            raise ValueError(
                f"Opção de billing '{key}' desconhecida (esperado um de {sorted(prefixes)})."
            )
        radio = self._page.locator(f'input[id^="{prefix}"][id$="-{group_suffix}"]').last
        if not await radio.is_enabled():
            raise ValueError(
                f"Opção de billing '{key}' está desabilitada para o SKU/região "
                "escolhidos (a própria calculadora restringe algumas combinações)."
            )
        await radio.click()

    async def export_estimate(self) -> str:
        """Clica em Compartilhar e devolve o link gerado pela calculadora.

        Fluxo real (menu "..." > Share): abre um dialog (`.share-modal`) com
        o link num `<textarea readonly name="link">` — não é um `<input>` nem
        um `<a href>`, então o valor sai por `input_value()`.

        Requer sessão autenticada. Atenção: deslogado, Share NÃO fica
        desabilitado (a suposição anterior aqui) — segue clicável e vira um
        link pro /auth/signin, então o clique navegava pra fora e só falhava
        10s depois, esperando um .share-modal que nunca ia montar. Por isso o
        pre-flight abaixo, que troca esse timeout por CalculatorSessionExpiredError.
        """
        if self._page is None:
            raise RuntimeError("Chame create_estimate() antes de export_estimate().")

        return await self._with_retry(self._export_estimate_once, "Export da estimativa")

    async def _export_estimate_once(self) -> str:
        """Uma tentativa do fluxo de share. Ver export_estimate()."""
        page = self._page
        assert page is not None

        await page.locator('[data-testid="stickyCostHeader__moreMenuButton"]').click()
        share_item = page.locator('[data-testid="moreOptionsMenuList__share"]')
        await share_item.wait_for(state="visible", timeout=8000)

        # Pre-flight: o link de login dentro do item Share é o sinal de sessão
        # morta mais próximo do ponto de uso — checar aqui evita clicar e
        # esperar o dialog que não vem.
        if await share_item.locator(_SHARE_LOGIN_LINK).count() > 0:
            raise CalculatorSessionExpiredError(
                'O menu Share está oferecendo "Log in to Share".'
            )

        await share_item.click()

        dialog = page.locator('.share-modal[role="dialog"]')
        link_field = dialog.locator('textarea[name="link"]')
        await link_field.wait_for(state="visible", timeout=10000)
        link = await link_field.input_value()

        await dialog.get_by_role("button", name="Done").click()
        return link
