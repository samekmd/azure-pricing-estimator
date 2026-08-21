"""Página falsa (duck-typed) que finge ser um Page/Locator do Playwright.

Existe porque `calculator_client.py` só toca o DOM através de
`self._page.locator(...)` e dos métodos do Locator — então dá para exercitar a
lógica INTEIRA do cliente (validação de campos, ordem de aplicação, escopo por
item, erros) sem navegador, sem rede e sem sessão. É o que torna o módulo mais
frágil do repo testável offline; a existência real dos seletores é conferida à
parte, pelo teste marcado `browser`.

O que se afirma nos testes é o registro de operações (`page.ops`): uma lista de
tuplas `(acao, alvo, *args)` onde `alvo` é a CADEIA de locators que produziu a
chamada (ex.: `div[id="vm-1-layout"] >> select[name="region"]`). Assim os testes
falam de "o que foi aplicado, onde", que é o contrato de verdade, e não do sabor
de locator usado para chegar lá.
"""

from __future__ import annotations

import re
from typing import Any

# Espelha o que a calculadora faz de verdade: clicar num picker cria um
# <div id="<slug>-<GUID>-layout"> novo no fim da lista de itens.
_PICKER_RE = re.compile(r'\[data-testid="(?P<slug>[a-z0-9-]+)-picker-item"\]')
_LAYOUT_SELECTOR = 'div[id$="-layout"]'
_NTH_RE = re.compile(r"^(?P<base>.+)\.nth\((?P<i>\d+)\)$")


class FakeLocator:
    """Locator falso: acumula a cadeia de seletores e registra as ações."""

    def __init__(self, page: "FakePage", chain: str) -> None:
        self._page = page
        self.chain = chain

    # --- encadeamento (não registra nada; só compõe o alvo) ----------------- #
    def _derive(self, suffix: str) -> "FakeLocator":
        return FakeLocator(self._page, f"{self.chain}{suffix}")

    @property
    def first(self) -> "FakeLocator":
        return self._derive(".first")

    @property
    def last(self) -> "FakeLocator":
        return self._derive(".last")

    def nth(self, index: int) -> "FakeLocator":
        return self._derive(f".nth({index})")

    def locator(self, selector: str) -> "FakeLocator":
        return self._derive(f" >> {selector}")

    def get_by_role(self, role: str, name: str | None = None) -> "FakeLocator":
        return self._derive(f" >> role={role}[name={name}]")

    # --- ações (registram; respostas vêm do FakePage) ---------------------- #
    async def count(self) -> int:
        return self._page.count_of(self.chain)

    async def click(self, **kwargs: Any) -> None:
        self._page.ops.append(("click", self.chain))
        self._page._maybe_add_item(self.chain)
        self._page.raise_if_scripted(self.chain, "click")

    async def fill(self, value: str) -> None:
        self._page.ops.append(("fill", self.chain, value))

    async def select_option(self, label: str | None = None, **kwargs: Any) -> None:
        self._page.ops.append(("select_option", self.chain, label))

    async def wait_for(self, **kwargs: Any) -> None:
        self._page.ops.append(("wait_for", self.chain))
        self._page.raise_if_scripted(self.chain, "wait_for")

    async def is_enabled(self) -> bool:
        return self._page.enabled.get(self.chain, True)

    async def input_value(self) -> str:
        return self._page.values.get(self.chain, "")

    async def get_attribute(self, name: str) -> str | None:
        return self._page.attribute_of(self.chain, name)

    async def inner_text(self) -> str:
        return self._page.texts.get(self.chain, "")


class FakePage:
    """Page falso. Configure `counts`/`attributes`/`enabled` antes de usar."""

    def __init__(self) -> None:
        self.ops: list[tuple] = []
        self.counts: dict[str, int] = {}
        self.attributes: dict[tuple[str, str], str] = {}
        self.values: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.enabled: dict[str, bool] = {}
        # Itens da estimativa, na ordem de inserção (ids dos containers).
        self.item_ids: list[str] = []
        # chain -> (acao, excecao) para simular timeout/erro do Playwright.
        self.scripted_errors: dict[str, tuple[str, Exception]] = {}
        self.closed = False

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_role(self, role: str, name: str | None = None) -> FakeLocator:
        return FakeLocator(self, f"role={role}[name={name}]")

    # --- simulação da lista de itens da estimativa ------------------------- #
    def count_of(self, chain: str) -> int:
        if chain == _LAYOUT_SELECTOR:
            return len(self.item_ids)
        if chain.startswith('div[id="'):
            item_id, _, resto = chain[len('div[id="') :].partition('"]')
            if item_id not in self.item_ids:
                return 0
            if not resto:
                return 1
            # Campo DENTRO de um painel existente: assume-se presente, que é o
            # caso normal. Um teste que queira simular campo/grupo ausente
            # (radio condicional, mudança de layout) escreve counts[chain] = 0.
            return self.counts.get(chain, 1)
        return self.counts.get(chain, 0)

    def attribute_of(self, chain: str, name: str) -> str | None:
        match = _NTH_RE.match(chain)
        if match and match.group("base") == _LAYOUT_SELECTOR and name == "id":
            index = int(match.group("i"))
            if index < len(self.item_ids):
                return self.item_ids[index]
            return None
        return self.attributes.get((chain, name))

    def _maybe_add_item(self, chain: str) -> None:
        """Clique num picker cria um item, como na calculadora real."""
        match = _PICKER_RE.search(chain)
        if match is None:
            return
        n = len(self.item_ids)
        guid = f"{n:08d}-0000-0000-0000-000000000000"
        self.item_ids.append(f"{match.group('slug')}-{guid}-layout")

    def raise_if_scripted(self, chain: str, action: str) -> None:
        scripted = self.scripted_errors.get(chain)
        if scripted and scripted[0] == action:
            raise scripted[1]

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.ops.append(("goto", url))

    async def close(self) -> None:
        self.closed = True

    # --- helpers de asserção usados pelos testes --------------------------- #
    def applied(self) -> list[tuple]:
        """Só as operações que MUDAM um campo (ignora cliques de navegação)."""
        return [op for op in self.ops if op[0] in ("fill", "select_option")]

    @staticmethod
    def field_of(chain: str) -> str:
        """Último seletor da cadeia, sem o sabor de locator (.last/.nth(i)).

        Normaliza de propósito: QUAL locator foi usado para chegar no campo é
        detalhe de implementação (`.last` hoje, escopo por container depois);
        o contrato que os testes travam é o campo e o valor.
        """
        trecho = chain.split(" >> ")[-1]
        for sufixo in (".last", ".first"):
            if trecho.endswith(sufixo):
                trecho = trecho[: -len(sufixo)]
        if ".nth(" in trecho:
            trecho = trecho.split(".nth(")[0]
        return trecho

    def fields_in_order(self) -> list[str]:
        """Nome dos campos aplicados, na ordem — para travar a ordem do painel."""
        return [self.field_of(op[1]) for op in self.applied()]

    def values_by_field(self) -> dict[str, Any]:
        """Campo -> valor aplicado (último, se aplicado mais de uma vez)."""
        return {self.field_of(op[1]): op[2] for op in self.applied()}
