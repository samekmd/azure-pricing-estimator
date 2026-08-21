"""Ciclo de vida do navegador no servidor MCP (offline, sem Playwright).

O cliente de calculadora é um singleton de módulo: antes ele era aberto na
primeira chamada e só morria junto com o processo — um Chromium aberto
indefinidamente, e órfão quando o cliente MCP matava o servidor.

Aqui trava-se o comportamento das três formas de fechá-lo (lifespan, tool
explícita e ociosidade) e, principalmente, a regra que protege o usuário:
**não fechar por ociosidade enquanto houver estimativa aberta**, porque a
estimativa só existe na página e fechar a destruiria em silêncio.
"""

from __future__ import annotations

import pytest

from azure_estimator_mcp import server


class FakeCalculator:
    """Dublê do AzureCalculatorClient — conta aberturas e fechamentos."""

    def __init__(self, **kwargs) -> None:
        self.entered = False
        self.exited = False
        self.authenticated = True

    async def __aenter__(self) -> "FakeCalculator":
        self.entered = True
        return self

    async def __aexit__(self, *exc) -> None:
        self.exited = True

    async def is_authenticated(self) -> bool:
        return self.authenticated

    async def create_estimate(self) -> None:
        pass

    async def export_estimate(self) -> str:
        return "https://azure.com/e/fake"


@pytest.fixture
def calculadora(monkeypatch):
    """Isola o estado global do módulo e injeta o dublê."""
    criados: list[FakeCalculator] = []

    def factory(**kwargs):
        client = FakeCalculator(**kwargs)
        criados.append(client)
        return client

    monkeypatch.setattr(server, "AzureCalculatorClient", factory)
    monkeypatch.setattr(server, "_calculator", None)
    monkeypatch.setattr(server, "_estimate_open", False)
    monkeypatch.setattr(server, "_last_used", 0.0)
    return criados


async def test_navegador_e_reaproveitado_entre_chamadas(calculadora):
    primeiro = await server._get_calculator()
    segundo = await server._get_calculator()
    assert primeiro is segundo
    assert len(calculadora) == 1, "não pode abrir um Chromium por chamada"


async def test_close_calculator_fecha_e_zera_o_singleton(calculadora):
    await server._get_calculator()
    resultado = await server.close_calculator()

    assert resultado["closed"] is True
    assert calculadora[0].exited is True
    assert server._calculator is None


async def test_close_calculator_sem_navegador_aberto_e_inofensivo(calculadora):
    resultado = await server.close_calculator()
    assert resultado["closed"] is False
    assert calculadora == []


async def test_ociosidade_nao_fecha_com_estimativa_aberta(calculadora, monkeypatch):
    """A regra que protege o trabalho do usuário.

    A estimativa existe SÓ na página; fechar o navegador com ela aberta
    descartaria tudo sem aviso.
    """
    await server._get_calculator()
    monkeypatch.setattr(server, "_estimate_open", True)
    monkeypatch.setattr(server, "_last_used", 0.0)

    # Muito além do timeout, e ainda assim não expira.
    assert server._idle_expired(server._IDLE_TIMEOUT_SECONDS * 100) is False


async def test_ociosidade_fecha_quando_nao_ha_estimativa(calculadora, monkeypatch):
    await server._get_calculator()
    monkeypatch.setattr(server, "_estimate_open", False)
    monkeypatch.setattr(server, "_last_used", 0.0)

    assert server._idle_expired(server._IDLE_TIMEOUT_SECONDS + 1) is True


async def test_ociosidade_respeita_o_tempo_configurado(calculadora, monkeypatch):
    await server._get_calculator()
    monkeypatch.setattr(server, "_last_used", 0.0)

    assert server._idle_expired(server._IDLE_TIMEOUT_SECONDS - 1) is False


async def test_ociosidade_sem_navegador_nao_tenta_fechar(calculadora):
    assert server._idle_expired(1e9) is False


async def test_create_estimate_bloqueia_o_fechamento_por_ociosidade(calculadora):
    await server.create_estimate()
    assert server._estimate_open is True
    assert server._idle_expired(1e9) is False


async def test_export_libera_o_navegador_para_fechar(calculadora):
    """Depois do export o link preserva a estimativa — pode fechar."""
    await server.create_estimate()
    link = await server.export_estimate()

    assert link == "https://azure.com/e/fake"
    assert server._estimate_open is False
    assert server._idle_expired(1e9) is True


async def test_lifespan_fecha_o_navegador_no_encerramento(calculadora):
    async with server._lifespan(server.mcp):
        await server._get_calculator()
        assert calculadora[0].exited is False

    assert calculadora[0].exited is True
    assert server._calculator is None


async def test_sessao_expirada_vira_erro_tipado(calculadora):
    client = await server._get_calculator()
    client.authenticated = False

    with pytest.raises(server.CalculatorAuthError, match="bootstrap_login"):
        await server.create_estimate()
