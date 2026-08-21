"""Cliente assíncrono da calculadora de preços da Azure, dirigido por Playwright.

Reutiliza o estado de sessão salvo pelo scripts/bootstrap_login.py para dirigir
a UI da calculadora já autenticado — a autenticação é delegada a um navegador
real (o jeito que a Microsoft desenhou: cookie + CSRF de uma sessão de verdade).

Os seletores usados abaixo foram mapeados ao vivo (DOM real, sessão
autenticada) em 06/08. Preferência de estabilidade: `data-testid` > `name`/
`id` de campo > classe Fluent UI (hash, evitada sempre que possível).
"""

from __future__ import annotations

import re
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

# CONTAINER DE UM ITEM — sondado ao vivo em 21/08. Cada serviço adicionado à
# estimativa vira um <div id="<slug>-<GUID>-layout"> que contém TODOS os campos
# daquele item e nada de outro item; `div[id$="-layout"]` bate exatamente na
# lista de itens (sem extras) e a ordem no DOM é a ordem de inserção.
#
# O <slug> é o prefixo do testid do picker ("virtual-machines-picker-item" ->
# "virtual-machines"), e o <GUID> é O MESMO que aparece nos ids dos radios de
# billing (radio-payg-<GUID>-computeBillingOption) — ou seja, escopar pelo
# container já escopa os radios de graça.
#
# Por que isso importa e não é cosmético: com duas VMs na estimativa o
# documento tem DOIS elementos com id="size" (id duplicado, HTML inválido mas
# real). Enquanto os campos eram resolvidos por `.last`, só dava para tocar o
# ÚLTIMO item — adicionar funcionava, editar não.
_ITEM_LAYOUT_SELECTOR = 'div[id$="-layout"]'
_ITEM_ID_RE = re.compile(
    r"^(?P<slug>.+)-"
    r"(?P<guid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})-layout$"
)
_SLUG_TO_SERVICE: dict[str, str] = {
    testid.removesuffix("-picker-item"): service
    for service, testid in _PICKER_TESTIDS.items()
}

# Âncora de "o painel do item terminou de montar": todo serviço tem exatamente
# um <select name="region"> no seu painel.
_ITEM_READY_ANCHOR = 'select[name="region"]'

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
# resolvidos dentro do container do item, não há ambiguidade de seletor; a
# ambiguidade é de leitura, daí este aviso. NÃO há campo de quantidade de
# contas de storage: para contas separadas, adicione o item mais de uma vez.
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
# Só DUAS opções, confirmadas por sondagem em 21/08 com o painel em General
# Purpose / Provisioned / Gen5: "Pay as you go" e "Bring Your Own License
# (Azure Hybrid Benefit)". Havia aqui um "savings_1yr" -> "radio-sv-one-year-"
# que NÃO existe no DOM: o grupo de licença não tem savings plan (só o de
# compute tem). Pedir essa chave morria num timeout de 30s do Playwright
# procurando um radio inexistente. Removido.
_SQL_SOFTWARE_BILLING_PREFIXES = {
    "license_included": "radio-payg-",  # "Pay as you go" = licença inclusa
    "azure_hybrid_benefit": "radio-ahb-",  # BYOL
}


# Widget de conta DA CALCULADORA (não o header global da Microsoft, que mostra
# "Sign in" mesmo logado). Deslogado, o container inteiro some do DOM — medido
# em 20/08 e reconfirmado em 21/08: #user-display e .calc-login com count=0 e um
# <div class="card-login"> "Log in to save cost estimates…" no lugar.
_ACCOUNT_WIDGET = "#user-display"
_LOGGED_OUT_CARD = "div.card-login"

_BOOTSTRAP_HINT = (
    "Rode novamente: uv run python mcp-server/scripts/bootstrap_login.py"
)


class CalculatorAuthError(RuntimeError):
    """A sessão salva não está (mais) logada na calculadora.

    Tipo próprio de propósito: quem chama precisa distinguir "refaça o
    bootstrap" de "algo quebrou", e essa é a falha operacional mais comum do
    projeto — não há renovação automática por design.
    """


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

        Validado nos DOIS estados: logado, #user-display fica visível;
        deslogado, #user-display e .calc-login somem do DOM (count=0 — o
        container inteiro é removido, não escondido) e aparece um
        <div class="card-login">. Capturado ao vivo em 20/08 e reconfirmado em
        21/08, quando a sessão expirou sozinha.

        ABRE UMA PÁGINA NOVA a cada chamada (goto + networkidle): use
        _page_is_authenticated() quando já houver uma página aberta.
        """
        if self._context is None:
            raise RuntimeError(
                "Cliente não inicializado. Use como context manager async "
                "(async with AzureCalculatorClient() as client: ...)."
            )

        page = await self._context.new_page()
        try:
            await page.goto(CALCULATOR_URL, wait_until="networkidle")
            user_display = page.locator(_ACCOUNT_WIDGET)
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

    def _item_root(self, item_id: str):
        """Locator da RAIZ de um item. Todos os campos são resolvidos dentro dela.

        Escopar por aqui é o que torna a estimativa endereçável: sem isso os
        campos eram resolvidos por `.last` e só o último item era alcançável.
        """
        assert self._page is not None
        return self._page.locator(f'div[id="{item_id}"]')

    @staticmethod
    def service_of_item(item_id: str) -> str:
        """Descobre o serviço a partir do id do container (o slug do prefixo)."""
        match = _ITEM_ID_RE.match(item_id)
        if match is None:
            raise ValueError(
                f"item_id {item_id!r} não tem o formato "
                "'<serviço>-<GUID>-layout' devolvido por add_line_item()."
            )
        slug = match.group("slug")
        service = _SLUG_TO_SERVICE.get(slug)
        if service is None:
            raise ValueError(
                f"Item do tipo {slug!r} não é um serviço mapeado "
                f"(conhecidos: {sorted(_SLUG_TO_SERVICE)})."
            )
        return service

    @staticmethod
    def _allowed_fields(service: str) -> tuple[set[str], set[str]]:
        """(selects simples, demais campos) aceitos por um serviço."""
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
        return select_fields, extra_fields

    def _validate_fields(self, service: str, config: dict[str, Any]) -> None:
        """Recusa campo sem seletor mapeado ANTES de tocar a página.

        Mesma regra dos resolvers em meters.py: não adivinhar. E a validação
        vem antes do clique de propósito — uma config inválida não deve deixar
        um item pela metade na estimativa.
        """
        select_fields, extra_fields = self._allowed_fields(service)
        unknown_fields = set(config) - select_fields - extra_fields
        if unknown_fields:
            raise NotImplementedError(
                f"Campo(s) {sorted(unknown_fields)} de '{service}' ainda não "
                f"têm seletor mapeado (aceitos: "
                f"{sorted(select_fields | extra_fields)})."
            )

    async def add_line_item(self, service: str, config: dict[str, Any]) -> str:
        """Adiciona um serviço à estimativa, aplica a config e devolve o item_id.

        `service` usa as mesmas chaves de RESOLVERS (meters.py) / _CATALOG
        (catalog.py): "vm", "storage", "sql".

        O retorno é o id do container do item recém-criado
        ("virtual-machines-<GUID>-layout"). Guarde-o: é o endereço do item para
        edit_line_item() — com vários itens do mesmo serviço na estimativa, é a
        única forma de dizer QUAL deles se quer mexer.

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
          e "blobBillingOption". ATENÇÃO: no painel de storage o "count" é a
          CAPACIDADE, não a quantidade de contas (na VM o mesmo name significa
          número de instâncias). Não há campo de quantidade de contas: para
          contas separadas, chame add_line_item() mais de uma vez.

        Campo desconhecido levanta NotImplementedError em vez de ser ignorado
        em silêncio — mesma regra dos resolvers em meters.py: não adivinhar.
        """
        if self._page is None:
            raise RuntimeError("Chame create_estimate() antes de add_line_item().")

        testid = _PICKER_TESTIDS.get(service)
        if testid is None:
            raise ValueError(
                f"Serviço '{service}' sem seletor mapeado "
                f"(esperado um de {sorted(_PICKER_TESTIDS)})."
            )
        self._validate_fields(service, config)

        # O testid aparece 2x no DOM (aba "Popular" + aba da categoria); só a
        # cópia visível é clicável.
        add_button = self._page.locator(f'[data-testid="{testid}"]:visible').first

        # O painel do item novo é montado de forma ASSÍNCRONA, e demora mais
        # conforme a estimativa cresce (medido ao vivo: ~1.1s com 3 itens já
        # na lista). Antes aqui havia uma espera fixa de 500ms — uma corrida:
        # se o painel ainda não existisse, o `.last` dos campos resolvia para o
        # item ANTERIOR e o select_option morria num timeout de 30s procurando
        # um rótulo que não existe naquele serviço (ex.: "Block Blob Storage"
        # no <select type> de um item SQL). Esperamos o painel APARECER, não o
        # relógio: o container de índice `antes` e, dentro dele, a âncora que
        # indica que os campos já foram renderizados.
        itens = self._page.locator(_ITEM_LAYOUT_SELECTOR)
        antes = await itens.count()
        await add_button.click()

        novo = itens.nth(antes)
        await novo.wait_for(state="attached", timeout=15000)
        await novo.locator(_ITEM_READY_ANCHOR).wait_for(state="attached", timeout=15000)

        item_id = await novo.get_attribute("id")
        if not item_id:
            raise RuntimeError(
                "O painel do item foi criado sem id — o layout da calculadora "
                "mudou; revalide _ITEM_LAYOUT_SELECTOR contra o DOM real."
            )

        await self._apply_fields(self._item_root(item_id), service, config)
        return item_id

    async def edit_line_item(self, item_id: str, config: dict[str, Any]) -> None:
        """Reaplica campos num item JÁ adicionado, endereçado pelo item_id.

        `item_id` é o que add_line_item() devolveu. O serviço é deduzido do
        próprio id (o slug do prefixo), então não há como pedir a edição de um
        item de VM com campos de storage por engano.

        Só os campos passados são tocados; o resto do item fica como está.
        """
        if self._page is None:
            raise RuntimeError("Chame create_estimate() antes de edit_line_item().")

        service = self.service_of_item(item_id)
        self._validate_fields(service, config)

        root = self._item_root(item_id)
        if await root.count() == 0:
            raise ValueError(
                f"Item {item_id!r} não está na estimativa aberta. Ele veio de "
                "outra estimativa, ou create_estimate() foi chamado de novo "
                "(o que recomeça do zero e invalida os ids anteriores)."
            )
        await self._apply_fields(root, service, config)

    async def _apply_fields(
        self, root, service: str, config: dict[str, Any]
    ) -> None:
        """Aplica os campos DENTRO da raiz de um item.

        A ordem de iteração do dict é preservada de propósito: trocar um select
        remonta as opções dos de baixo (mudar vcoreTier refaz a lista de
        instanceSize; a unidade de storage tem que vir antes da capacidade,
        senão 500 é lido na unidade errada). Quem garante a ordem certa é o
        config_translate.py, que emite do grosso para o fino.
        """
        select_fields, _ = self._allowed_fields(service)

        for field, value in config.items():
            if field in select_fields:
                await root.locator(f'select[name="{field}"]').select_option(label=value)
            elif field in _TEXT_INPUT_FIELDS:
                await root.locator(f'input[name="{field}"]').fill(str(value))
            elif field == "hoursFactor":
                await root.locator('select[name="hoursFactor"]').select_option(
                    label=value
                )
            elif field == _VM_SIZE_FIELD:
                await self._apply_vm_size(root, value)
            elif field == "computeBillingOption":
                await self._click_billing_radio(
                    root, _VM_COMPUTE_BILLING_PREFIXES, value, "computeBillingOption"
                )
            elif field == "osBillingOption":
                await self._click_billing_radio(
                    root, _VM_OS_BILLING_PREFIXES, value, "osBillingOption"
                )
            elif field == "databaseBillingOption":
                await self._click_billing_radio(
                    root, _SQL_DATABASE_BILLING_PREFIXES, value, "databaseBillingOption"
                )
            elif field == "softwareBillingOption":
                await self._click_billing_radio(
                    root, _SQL_SOFTWARE_BILLING_PREFIXES, value, "softwareBillingOption"
                )
            elif field == "blobBillingOption":
                await self._click_billing_radio(
                    root, _STORAGE_BILLING_PREFIXES, value, "blobBillingOption"
                )

    async def _apply_vm_size(self, root, search_text: str) -> None:
        """Digita no combobox INSTANCE (#size) do item e clica a sugestão.

        Widget é um react-select: digitar dispara uma busca assíncrona que
        renderiza <div role="option"> com o SKU completo (ex. "D4s v3: 4
        vCPUs, ..."). `search_text` deve ser específico o bastante pra
        deixar uma opção só (ex. o skuName, não só a série), senão o
        `.first` pode acabar escolhendo o SKU errado.

        As opções são renderizadas DENTRO do container do item (sondado em
        21/08: não é um portal no <body>), então o escopo por item vale para
        elas também — importante porque com duas VMs na estimativa existem
        dois elementos com id="size" no documento.
        """
        size_input = root.locator("#size")
        await size_input.click()
        await size_input.fill(search_text)
        option = root.locator('[role="option"]:visible').first
        await option.wait_for(state="visible", timeout=10000)
        await option.click()

    async def _click_billing_radio(
        self, root, prefixes: dict[str, str], key: str, group_suffix: str
    ) -> None:
        """Clica o radio de billing correspondente a `key`, dentro do item.

        O id real embute o GUID do item
        (radio-<prefixo>-<GUID>-computeBillingOption), então localizamos pelo
        prefixo estável do id, não pelo texto do label (o label de savings plan
        inclui um "~X% discount" que varia por SKU/região). Como a busca é
        feita dentro da raiz do item, o GUID não precisa ser reconstruído.

        Algumas opções (ex.: "1 year reserved") ficam desabilitadas pela
        própria calculadora dependendo do SKU/região escolhido ("1 year
        reserved option is not available for your instance selection" — visto
        ao vivo com D4s v3). Checa is_enabled() antes de clicar pra falhar
        rápido e com uma mensagem clara, em vez de esperar o timeout padrão
        do Playwright tentando clicar num elemento desabilitado.
        """
        prefix = prefixes.get(key)
        if prefix is None:
            raise ValueError(
                f"Opção de billing '{key}' desconhecida (esperado um de {sorted(prefixes)})."
            )
        radio = root.locator(f'input[id^="{prefix}"][id$="-{group_suffix}"]')
        # Alguns grupos são CONDICIONAIS: existem ou não conforme o resto da
        # config. Medido em 21/08: `osBillingOption` (Azure Hybrid Benefit do
        # SO) só é montado quando operatingSystem=Windows — com Linux o grupo
        # inteiro some do DOM. Sem esta checagem, pedir a opção nesse estado
        # morre num timeout de 30s do Playwright em vez de explicar o motivo.
        if await radio.count() == 0:
            raise ValueError(
                f"O grupo '{group_suffix}' não existe no painel deste item. "
                "Ou a opção não se aplica à config escolhida (ex.: "
                "osBillingOption só existe para Windows), ou o layout da "
                "calculadora mudou."
            )
        if not await radio.is_enabled():
            raise ValueError(
                f"Opção de billing '{key}' está desabilitada para o SKU/região "
                "escolhidos (a própria calculadora restringe algumas combinações)."
            )
        await radio.click()

    async def _page_is_authenticated(self) -> bool:
        """Checa a sessão NA PÁGINA JÁ ABERTA — sem abrir outra nem navegar.

        is_authenticated() faz um goto + networkidle numa página nova, o que é
        caro para uma checagem que acontece logo antes de um clique numa página
        que já está carregada.
        """
        assert self._page is not None
        return await self._page.locator(_ACCOUNT_WIDGET).count() > 0

    async def export_estimate(self) -> str:
        """Compartilha a estimativa aberta e devolve o link gerado.

        Fluxo real (menu "..." > Share): abre um dialog (`.share-modal`) com
        o link num `<textarea readonly name="link">` — não é um `<input>` nem
        um `<a href>`, então o valor sai por `input_value()`.

        EXIGE SESSÃO AUTENTICADA, e a checagem é feita aqui. Cuidado que já
        custou caro: o item "Share" do menu NÃO fica desabilitado quando
        deslogado. Medido ao vivo: ele continua `visible=True enabled=True`,
        com o texto "Share\nLog in to Share" — o "Log in to Share" é rótulo,
        não `disabled`. Sem esta checagem, chamar deslogado clicava, o
        `.share-modal` nunca abria e o método morria num TimeoutError de 10s do
        Playwright, contrariando a disciplina de falha limpa do resto do
        projeto.
        """
        if self._page is None:
            raise RuntimeError("Chame create_estimate() antes de export_estimate().")

        if not await self._page_is_authenticated():
            raise CalculatorAuthError(
                "Não dá para compartilhar a estimativa: a sessão da calculadora "
                f"expirou ou não está logada. {_BOOTSTRAP_HINT}"
            )

        page = self._page
        try:
            await page.locator(
                '[data-testid="stickyCostHeader__moreMenuButton"]'
            ).click()
            await page.locator('[data-testid="moreOptionsMenuList__share"]').click()

            dialog = page.locator('.share-modal[role="dialog"]')
            link_field = dialog.locator('textarea[name="link"]')
            await link_field.wait_for(state="visible", timeout=10000)
            link = await link_field.input_value()
        except PlaywrightTimeoutError as exc:
            # Cinto e suspensório: a checagem acima cobre o caso conhecido
            # (deslogado); isto cobre mudança de layout do menu/dialog, que
            # também chegaria como timeout opaco.
            raise RuntimeError(
                "O diálogo de compartilhamento não abriu no tempo esperado. "
                "Se a sessão estiver válida, os seletores do menu Share podem "
                f"ter mudado no layout da calculadora. Detalhe: {exc}"
            ) from exc

        await dialog.get_by_role("button", name="Done").click()
        return link
