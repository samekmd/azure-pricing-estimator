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
        "storageUnits",  # unidade da capacidade: GB/TB
        "blobDataRetrievalUnits",
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

# Idem para storage. ATENÇÃO ao "count": o name é o MESMO da VM, mas o
# significado é outro — na VM é número de instâncias, aqui é a CAPACIDADE
# (em GB ou TB, conforme o <select storageUnits>). Como os campos são
# resolvidos dentro do painel do item (`.last`), não há ambiguidade de
# seletor; a ambiguidade é de leitura, daí este aviso.
_STORAGE_TEXT_INPUT_FIELDS = {
    "count",  # capacidade
    "blobWriteOperations",
    "blobCreateContainerOperations",
    "blobReadOperations",
    "blobOtherOperations",
    "blobDataRetrieval",
}
_TEXT_INPUT_FIELDS = _VM_TEXT_INPUT_FIELDS | _STORAGE_TEXT_INPUT_FIELDS

# Billing do storage. Diferente do SQL, o default aqui JÁ é "Pay as you go"
# (confirmado ao vivo em 20/08) — mas o campo é emitido explicitamente do
# mesmo jeito, pela mesma razão: default não é escolha.
_STORAGE_BILLING_PREFIXES = {
    "payg": "radio-payg-",
    "reserved_1yr": "radio-one-year-",
    "reserved_3yr": "radio-three-year-",
}

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

# SQL Database tem DOIS grupos de radio, e os dois vêm com um default que NÃO
# é pay-as-you-go — medido ao vivo em 20/08:
#   databaseBillingOption -> "3 year reserved (~55% discount)"
#   softwareBillingOption -> "Bring Your Own License (Azure Hybrid Benefit)"
# Ou seja: um item de SQL adicionado sem tocar nesses radios é precificado como
# reserva de 3 anos SEM licença — ~53% abaixo do preço on-demand que a Retail
# Prices API devolve. Não dá para deixar no default; ver config_translate.py.
_SQL_DATABASE_BILLING_PREFIXES = {
    "payg": "radio-payg-",
    "savings_1yr": "radio-sv-one-year-",
    "savings_3yr": "radio-sv-three-year-",
    "reserved_1yr": "radio-one-year-",
    "reserved_3yr": "radio-three-year-",
}
_SQL_SOFTWARE_BILLING_PREFIXES = {
    "license_included": "radio-payg-",  # "Pay as you go" = licença inclusa
    "savings_1yr": "radio-sv-one-year-",
    "azure_hybrid_benefit": "radio-ahb-",  # BYOL
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

        - Só para "sql": "databaseBillingOption" (payg/savings/reserved) e
          "softwareBillingOption" (license_included/azure_hybrid_benefit).
          NÃO omita os dois: os defaults da calculadora são "3 year reserved"
          e "Azure Hybrid Benefit", que precificam ~53% abaixo do on-demand.

        - Só para "storage": _STORAGE_TEXT_INPUT_FIELDS (capacidade em
          "count" + unidade em "storageUnits", e os contadores de operações)
          e "blobBillingOption".

        Para storage, a QUANTIDADE de contas ainda não Um campo desconhecido levanta NotImplementedError
        em vez de ser ignorado silenciosamente — mesma regra dos resolvers em
        meters.py: não adivinhar.

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
        elif service == "sql":
            extra_fields = {"databaseBillingOption", "softwareBillingOption"}
        elif service == "storage":
            extra_fields = _STORAGE_TEXT_INPUT_FIELDS | {"blobBillingOption"}

        unknown_fields = set(config) - select_fields - extra_fields
        if unknown_fields:
            raise NotImplementedError(
                f"Campo(s) {sorted(unknown_fields)} de '{service}' ainda não "
                "têm seletor mapeado (instância/quantidade/billing de "
                "storage e sql, por exemplo, são follow-up)."
            )

        # O testid aparece 2x no DOM (aba "Popular" + aba da categoria); só a
        # cópia visível é clicável.
        add_button = self._page.locator(f'[data-testid="{testid}"]:visible').first

        # O painel do item novo é montado de forma ASSÍNCRONA, e demora mais
        # conforme a estimativa cresce (medido ao vivo: ~1.1s com 3 itens já
        # na lista). Antes aqui havia uma espera fixa de 500ms — uma corrida:
        # se o painel ainda não existisse, o `.last` dos campos abaixo
        # resolvia para o item ANTERIOR e o select_option morria num timeout
        # de 30s procurando um rótulo que não existe naquele serviço (ex.:
        # "Block Blob Storage" no <select type> de um item SQL). Agora
        # esperamos o painel APARECER, não o relógio: contamos as âncoras
        # antes do clique e aguardamos a de índice `antes` existir. Todos os
        # três serviços têm um <select name="region"> no painel, então ela
        # serve de âncora comum.
        ancora = 'select[name="region"]'
        antes = await self._page.locator(ancora).count()
        await add_button.click()
        await self._page.locator(ancora).nth(antes).wait_for(
            state="attached", timeout=15000
        )

        for field, value in config.items():
            if field in select_fields:
                select = self._page.locator(f'select[name="{field}"]').last
                await select.select_option(label=value)
            elif field in _TEXT_INPUT_FIELDS:
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
            elif field == "databaseBillingOption":
                await self._click_billing_radio(
                    _SQL_DATABASE_BILLING_PREFIXES, value, "databaseBillingOption"
                )
            elif field == "softwareBillingOption":
                await self._click_billing_radio(
                    _SQL_SOFTWARE_BILLING_PREFIXES, value, "softwareBillingOption"
                )
            elif field == "blobBillingOption":
                await self._click_billing_radio(
                    _STORAGE_BILLING_PREFIXES, value, "blobBillingOption"
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
