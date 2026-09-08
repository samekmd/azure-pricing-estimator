"""Retry do cliente da calculadora (Fase 3, sem rede e sem browser).

Cobre a única garantia que o retry precisa dar e que nenhum outro teste do
módulo cobre: timeout transitório é reexecutado com backoff, e o que NÃO é
transitório (sessão morta, config inválida) passa direto, sem esperar à toa.

A detecção de sessão e a aplicação de campos são exercidas em
test_calculator_client.py; aqui só entra o comportamento de reexecução.
"""

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from azure_estimator_mcp.azure.calculator_client import (
    AzureCalculatorClient,
    CalculatorAuthError,
)

from .fake_page import FakePage


@pytest.fixture
def client():
    # Sem __aenter__: nada aqui precisa de browser.
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


async def test_sucesso_de_primeira_nao_dorme(client, sleeps):
    async def acao():
        return "link"

    assert await client._with_retry(acao, "Export") == "link"
    assert sleeps == []


async def test_se_recupera_de_timeout_transitorio(client, sleeps):
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


async def test_desiste_citando_a_operacao_e_o_ultimo_erro(client, sleeps):
    async def acao():
        raise PlaywrightTimeoutError("share-modal não apareceu")

    with pytest.raises(PlaywrightTimeoutError) as exc:
        await client._with_retry(acao, "Export da estimativa")

    msg = str(exc.value)
    assert "Export da estimativa" in msg
    assert "3 tentativas" in msg
    assert "share-modal não apareceu" in msg  # o erro original não se perde


async def test_sessao_morta_nao_e_retentada(client, sleeps):
    """Repetir em sessão expirada só atrasa a mensagem de refazer o bootstrap."""
    tentativas = []

    async def acao():
        tentativas.append(1)
        raise CalculatorAuthError("sessão expirada")

    with pytest.raises(CalculatorAuthError):
        await client._with_retry(acao, "Export")

    assert len(tentativas) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "erro", [ValueError("SKU indisponível"), NotImplementedError("campo sem seletor")]
)
async def test_erros_de_config_passam_direto(client, sleeps, erro):
    tentativas = []

    async def acao():
        tentativas.append(1)
        raise erro

    with pytest.raises(type(erro)):
        await client._with_retry(acao, "Add line item")

    assert len(tentativas) == 1
    assert sleeps == []


# --- integração do retry com o fluxo de share ------------------------------


async def test_export_reexecuta_o_fluxo_de_share_inteiro(client, sleeps, monkeypatch):
    """Uma tentativa que morre no meio não deixa a próxima num estado torto."""
    page = FakePage()
    page.counts["#user-display"] = 1  # sessão válida
    page.values['.share-modal[role="dialog"] >> textarea[name="link"]'] = (
        "https://azure.com/e/abc"
    )
    client._page = page

    tentativas = []
    original = client._open_share_dialog

    async def falha_uma_vez():
        tentativas.append(1)
        if len(tentativas) == 1:
            raise PlaywrightTimeoutError("dialog não abriu")
        return await original()

    monkeypatch.setattr(client, "_open_share_dialog", falha_uma_vez)

    assert await client.export_estimate() == "https://azure.com/e/abc"
    assert len(tentativas) == 2
    assert sleeps == [0.01]


async def test_export_deslogado_falha_antes_de_qualquer_tentativa(client, sleeps):
    """A checagem de sessão vem antes do retry: nada de 3x numa sessão morta."""
    page = FakePage()
    page.counts["#user-display"] = 0  # deslogado
    client._page = page

    with pytest.raises(CalculatorAuthError):
        await client.export_estimate()

    assert sleeps == []
    assert page.ops == []  # nem chegou a abrir o menu
