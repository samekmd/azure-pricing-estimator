"""Testes de robustez do export da calculadora (Fase 3, sem rede e sem browser).

Cobre as duas garantias que o fluxo de export passou a dar:
- retry com backoff exponencial em timeout transitório da UI;
- sessão expirada falha rápido, sem retry, com instrução de rebootstrap;
- campo de config sem seletor: estrito por padrão, parcial explícito com
  strict=False;
- estado de sessão decidido por dois sinais exclusivos, com o caso "nenhum dos
  dois" virando erro em vez de palpite.

Nada aqui sobe Playwright: _with_retry recebe corotinas comuns e o fluxo de
share é exercido contra dublês de Page/Locator com só o necessário da API.
"""

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from azure_estimator_mcp.azure.calculator_client import (
    _SIGN_IN_BUTTON,
    _USER_DISPLAY,
    AzureCalculatorClient,
    CalculatorAuthStateUnknownError,
    CalculatorSessionExpiredError,
)


@pytest.fixture
def client():
    # Sem __aenter__: nenhum teste daqui precisa de browser.
    return AzureCalculatorClient(max_retries=2, backoff_base=0.01)


@pytest.fixture
def sleeps(monkeypatch):
    """Captura os intervalos de backoff em vez de dormir de verdade."""
    registrados: list[float] = []

    async def fake_sleep(segundos):
        registrados.append(segundos)

    monkeypatch.setattr(
        "azure_estimator_mcp.azure.calculator_client.asyncio.sleep", fake_sleep
    )
    return registrados


# --- _with_retry -----------------------------------------------------------


async def test_retry_devolve_o_resultado_da_primeira_tentativa_boa(client, sleeps):
    async def acao():
        return "link"

    assert await client._with_retry(acao, "Export") == "link"
    assert sleeps == []  # sucesso de primeira não dorme


async def test_retry_se_recupera_de_timeout_transitorio(client, sleeps):
    tentativas = []

    async def acao():
        tentativas.append(1)
        if len(tentativas) < 3:
            raise PlaywrightTimeoutError("timeout")
        return "link"

    assert await client._with_retry(acao, "Export") == "link"
    assert len(tentativas) == 3


async def test_backoff_e_exponencial_na_convencao_do_retail_client(client, sleeps):
    """backoff_base * 2**tentativa — mesma fórmula do retry HTTP do projeto."""

    async def acao():
        raise PlaywrightTimeoutError("timeout")

    with pytest.raises(PlaywrightTimeoutError):
        await client._with_retry(acao, "Export")

    assert sleeps == [0.01, 0.02]  # max_retries=2 -> 3 tentativas, 2 esperas


async def test_retry_desiste_e_cita_a_operacao_e_o_ultimo_erro(client, sleeps):
    async def acao():
        raise PlaywrightTimeoutError("share-modal não apareceu")

    with pytest.raises(PlaywrightTimeoutError) as exc:
        await client._with_retry(acao, "Export da estimativa")

    msg = str(exc.value)
    assert "Export da estimativa" in msg
    assert "3 tentativas" in msg
    assert "share-modal não apareceu" in msg  # erro original não se perde


async def test_sessao_expirada_nao_e_retentada(client, sleeps):
    """Repetir em sessão morta só multiplica espera e atrasa a mensagem útil."""
    tentativas = []

    async def acao():
        tentativas.append(1)
        raise CalculatorSessionExpiredError()

    with pytest.raises(CalculatorSessionExpiredError):
        await client._with_retry(acao, "Export")

    assert len(tentativas) == 1
    assert sleeps == []


@pytest.mark.parametrize("erro", [ValueError("SKU indisponível"), NotImplementedError()])
async def test_erros_de_config_passam_direto_sem_retry(client, sleeps, erro):
    tentativas = []

    async def acao():
        tentativas.append(1)
        raise erro

    with pytest.raises(type(erro)):
        await client._with_retry(acao, "Add line item")

    assert len(tentativas) == 1
    assert sleeps == []


# --- dublês mínimos de Playwright ------------------------------------------


class FakeLocator:
    """Locator com só o que o fluxo de share chama."""

    def __init__(self, page, seletor):
        self._page = page
        self._seletor = seletor

    def locator(self, seletor):
        return FakeLocator(self._page, f"{self._seletor} {seletor}")

    # A cadeia .first/.last do Playwright não muda nada nos dublês.
    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        return self._page.contagens.get(self._seletor, 0)

    async def click(self, **kwargs):
        self._page.cliques.append(self._seletor)

    async def wait_for(self, **kwargs):
        if self._seletor in self._page.nunca_aparecem:
            raise PlaywrightTimeoutError(f"timeout esperando {self._seletor}")

    async def input_value(self):
        return self._page.link

    async def select_option(self, **kwargs):
        self._page.aplicados.append(self._seletor)

    async def fill(self, valor):
        self._page.aplicados.append(self._seletor)

    async def is_enabled(self):
        return True

    def get_by_role(self, role, name=None):
        return FakeLocator(self._page, f"{self._seletor} {role}:{name}")


class FakePage:
    def __init__(self, contagens=None, nunca_aparecem=(), link="https://calc/x"):
        self.contagens = contagens or {}
        self.nunca_aparecem = set(nunca_aparecem)
        self.link = link
        self.cliques: list[str] = []
        self.aplicados: list[str] = []

    def locator(self, seletor):
        return FakeLocator(self, seletor)

    async def wait_for_timeout(self, ms):
        pass

    async def goto(self, url, **kwargs):
        pass

    async def close(self):
        self.fechada = True


class FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page


_SHARE = '[data-testid="moreOptionsMenuList__share"]'
_LOGIN_LINK = f'{_SHARE} a[href*="/auth/signin"]'


async def test_export_devolve_o_link_com_sessao_valida(client):
    client._page = FakePage(link="https://azure.com/estimate/abc")
    assert await client.export_estimate() == "https://azure.com/estimate/abc"


async def test_export_detecta_sessao_expirada_antes_de_clicar_em_share(client, sleeps):
    """Deslogado o item Share vira link de login: falha antes do clique.

    Confirmado ao vivo em 21/08 — Share NÃO fica desabilitado, então sem esse
    pre-flight o clique navega pro /auth/signin e o erro só vinha 10s depois,
    como timeout genérico, multiplicado pelo retry.
    """
    page = FakePage(contagens={_LOGIN_LINK: 1})
    client._page = page

    with pytest.raises(CalculatorSessionExpiredError) as exc:
        await client.export_estimate()

    assert "bootstrap-login" in str(exc.value)
    assert _SHARE not in page.cliques  # nem chegou a clicar
    assert sleeps == []  # e nem tentou de novo


async def test_export_exige_create_estimate_antes(client):
    with pytest.raises(RuntimeError, match="create_estimate"):
        await client.export_estimate()


# --- fallback de campos não mapeados (strict) ------------------------------


async def test_add_line_item_estrito_e_o_padrao(client):
    """Sem strict=False, campo desconhecido continua levantando — não adivinhar."""
    client._page = FakePage()

    with pytest.raises(NotImplementedError) as exc:
        await client.add_line_item("storage", {"region": "East US", "capacity": 500})

    msg = str(exc.value)
    assert "capacity" in msg
    assert "strict=False" in msg  # o erro aponta a saída


async def test_add_line_item_nao_estrito_aplica_o_resto_e_reporta(client):
    client._page = FakePage()

    resultado = await client.add_line_item(
        "storage",
        {"region": "East US", "redundancy": "LRS", "capacity": 500, "blobBilling": "x"},
        strict=False,
    )

    assert resultado.applied_fields == ["redundancy", "region"]
    assert resultado.ignored_fields == ["blobBilling", "capacity"]
    assert resultado.complete is False


async def test_add_line_item_nao_toca_no_campo_ignorado(client):
    """O parcial precisa ser parcial de verdade: nada de fill/select no chute."""
    page = FakePage()
    client._page = page

    await client.add_line_item(
        "storage", {"region": "East US", "capacity": 500}, strict=False
    )

    assert any("region" in sel for sel in page.aplicados)
    assert not any("capacity" in sel for sel in page.aplicados)


async def test_config_toda_mapeada_e_completa(client):
    client._page = FakePage()

    resultado = await client.add_line_item("storage", {"region": "East US"})

    assert resultado.complete is True
    assert resultado.ignored_fields == []
    assert resultado.service == "storage"


async def test_servico_sem_picker_falha_mesmo_em_modo_parcial(client):
    """strict=False afrouxa campo, não serviço: sem picker não há o que aplicar."""
    client._page = FakePage()

    with pytest.raises(ValueError, match="redis"):
        await client.add_line_item("redis", {"region": "East US"}, strict=False)


# --- estado de sessão: dois sinais exclusivos ------------------------------


def _client_com_dom(client, presentes, nunca_aparecem=()):
    """Monta um cliente cujo DOM tem só os seletores de `presentes`."""
    page = FakePage(
        contagens={sel: 1 for sel in presentes}, nunca_aparecem=nunca_aparecem
    )
    client._context = FakeContext(page)
    return page


_QUALQUER_SINAL = f"{_SIGN_IN_BUTTON}, {_USER_DISPLAY}"


async def test_botao_de_login_presente_significa_deslogado(client):
    """Estado confirmado ao vivo em 21/08 com contexto limpo."""
    _client_com_dom(client, [_SIGN_IN_BUTTON])
    assert await client.is_authenticated() is False


async def test_widget_de_conta_presente_significa_logado(client):
    """Estado confirmado ao vivo em 21/08 com storage_state recém-bootstrapado."""
    _client_com_dom(client, [_USER_DISPLAY])
    assert await client.is_authenticated() is True


async def test_nenhum_dos_dois_sinais_nao_vira_palpite(client):
    """DOM mudou: erro de manutenção, não "deslogado" calado."""
    _client_com_dom(client, [], nunca_aparecem=[_QUALQUER_SINAL])

    with pytest.raises(CalculatorAuthStateUnknownError) as exc:
        await client.is_authenticated()

    # A mensagem precisa mandar revalidar seletor, não refazer bootstrap.
    assert "revalidar" in str(exc.value)
    assert "bootstrap" not in str(exc.value)


async def test_estado_indeterminado_nao_e_confundido_com_expirado(client):
    """São exceções distintas porque a ação corretiva é distinta."""
    assert not issubclass(
        CalculatorAuthStateUnknownError, CalculatorSessionExpiredError
    )
    assert not issubclass(
        CalculatorSessionExpiredError, CalculatorAuthStateUnknownError
    )


async def test_na_duvida_os_dois_presentes_conta_como_deslogado(client):
    """Frame de transição: mandar refazer bootstrap é mais barato que falhar
    lá na frente, no export, com a estimativa já montada."""
    _client_com_dom(client, [_SIGN_IN_BUTTON, _USER_DISPLAY])
    assert await client.is_authenticated() is False


async def test_ensure_authenticated_levanta_com_instrucao(client):
    _client_com_dom(client, [_SIGN_IN_BUTTON])

    with pytest.raises(CalculatorSessionExpiredError, match="bootstrap-login"):
        await client.ensure_authenticated()


async def test_ensure_authenticated_passa_logado(client):
    _client_com_dom(client, [_USER_DISPLAY])
    assert await client.ensure_authenticated() is None
