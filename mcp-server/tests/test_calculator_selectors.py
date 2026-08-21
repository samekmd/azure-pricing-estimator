"""Os seletores mapeados ainda existem no DOM real da calculadora? (`browser`)

Contraparte viva dos testes offline de test_calculator_client.py: aqueles travam
a LÓGICA com uma página falsa, este confere que os SELETORES continuam existindo
na página de verdade. É o que pega uma mudança de layout do lado da Microsoft
cedo, em vez de dela aparecer como um timeout de 30s no meio de uma demo.

Marcado `browser` (não `integration`, que neste projeto significa "bate na
Retail Prices API"): precisa de Chromium instalado, não de rede na API de preço.

**Não exige sessão autenticada** — montar uma estimativa funciona deslogado; só
salvar/compartilhar exige login. E **não tem efeito colateral**: nada de Save,
nada de Share, nenhuma estimativa publicada.

Rodar com: `make test-browser` (ou `uv run pytest -m browser`).
"""

from __future__ import annotations

import socket

import pytest

from azure_estimator_mcp.azure.calculator_client import (
    _ITEM_ID_RE,
    _PICKER_TESTIDS,
    _SIMPLE_SELECT_FIELDS,
    _SQL_DATABASE_BILLING_PREFIXES,
    _SQL_SOFTWARE_BILLING_PREFIXES,
    _STORAGE_BILLING_PREFIXES,
    _STORAGE_TEXT_INPUT_FIELDS,
    _VM_COMPUTE_BILLING_PREFIXES,
    _VM_TEXT_INPUT_FIELDS,
    AzureCalculatorClient,
)
from azure_estimator_mcp.azure.config_translate import translate_config

pytestmark = pytest.mark.browser

# Configs no vocabulário do RESOLVER (como vêm dos padrões da Skill), para o
# teste exercitar o caminho inteiro: config -> tradutor -> UI real.
_COMPONENTES = {
    "vm": ({"armSkuName": "Standard_D2s_v3", "region": "eastus"}, {"hours": 730}),
    "sql": (
        {"region": "eastus", "tier": "General Purpose", "hardware": "Gen5", "vCores": 2},
        {},
    ),
    "storage": (
        {"region": "eastus", "tier": "Hot", "redundancy": "LRS"},
        {"gb": 500},
    ),
}


def _tem_rede(host: str = "azure.microsoft.com", port: int = 443) -> bool:
    try:
        socket.create_connection((host, port), timeout=5).close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def _rede():
    if not _tem_rede():
        pytest.skip("sem rede para alcançar a calculadora")


@pytest.fixture
async def calculadora(_rede, tmp_path):
    """Cliente com estimativa aberta. Sessão NÃO é necessária."""
    # O cliente exige o arquivo de storage_state; deslogado serve, e é o que
    # torna este teste rodável sem bootstrap.
    estado = tmp_path / "storage_state.json"
    estado.write_text('{"cookies": [], "origins": []}')

    client = AzureCalculatorClient(storage_state_path=str(estado), headless=True)
    await client.__aenter__()
    try:
        await client.create_estimate()
        yield client
    finally:
        await client.__aexit__(None, None, None)


def _campos_esperados(service: str) -> list[str]:
    """Seletores que o cliente promete saber achar dentro do painel do item."""
    seletores = [f'select[name="{c}"]' for c in sorted(_SIMPLE_SELECT_FIELDS[service])]
    if service == "vm":
        seletores += [f'input[name="{c}"]' for c in sorted(_VM_TEXT_INPUT_FIELDS)]
        seletores += ["#size", 'select[name="hoursFactor"]']
        seletores += [
            f'input[id^="{p}"][id$="-computeBillingOption"]'
            for p in _VM_COMPUTE_BILLING_PREFIXES.values()
        ]
        # osBillingOption NÃO entra aqui: o grupo é CONDICIONAL — só é montado
        # quando operatingSystem=Windows (medido em 21/08; com Linux ele some
        # do DOM inteiro). Como o componente de referência é Linux, ele tem
        # teste próprio, abaixo.
    elif service == "storage":
        seletores += [f'input[name="{c}"]' for c in sorted(_STORAGE_TEXT_INPUT_FIELDS)]
        seletores += [
            f'input[id^="{p}"][id$="-blobBillingOption"]'
            for p in _STORAGE_BILLING_PREFIXES.values()
        ]
    elif service == "sql":
        seletores += [
            f'input[id^="{p}"][id$="-databaseBillingOption"]'
            for p in _SQL_DATABASE_BILLING_PREFIXES.values()
        ]
        seletores += [
            f'input[id^="{p}"][id$="-softwareBillingOption"]'
            for p in _SQL_SOFTWARE_BILLING_PREFIXES.values()
        ]
    return seletores


@pytest.mark.parametrize("service", sorted(_PICKER_TESTIDS))
async def test_todos_os_seletores_mapeados_existem_no_painel(calculadora, service):
    """Cada campo mapeado tem que resolver DENTRO do container do item."""
    config, usage = _COMPONENTES[service]
    traducao = translate_config(service, config, usage)

    # Se um rótulo do tradutor não existir como <option>, select_option estoura
    # aqui — ou seja, este passo já é o teste dos rótulos de config_translate.
    item_id = await calculadora.add_line_item(service, traducao.ui_config)
    assert _ITEM_ID_RE.match(item_id), f"id de item inesperado: {item_id}"

    root = calculadora._item_root(item_id)
    faltando = [
        seletor
        for seletor in _campos_esperados(service)
        if await root.locator(seletor).count() == 0
    ]
    assert not faltando, f"seletores sumiram do painel de '{service}': {faltando}"


async def test_menu_de_compartilhar_ainda_existe(calculadora):
    """Confere a existência dos testids do Share — sem clicar (sem publicar)."""
    page = calculadora._page
    for testid in (
        "stickyCostHeader__moreMenuButton",
        # O item do menu só é montado depois de abrir o menu, então aqui basta
        # o botão que o abre; o resto do fluxo é coberto pelo teste offline.
    ):
        assert await page.locator(f'[data-testid="{testid}"]').count() >= 1


async def test_edita_o_primeiro_item_com_um_segundo_na_lista(calculadora):
    """A prova viva de que a estimativa virou endereçável.

    Com duas VMs, o `.last` de antes só alcançava a segunda. Agora edita-se a
    PRIMEIRA e confere-se, lendo o DOM de volta, que só ela mudou.
    """
    config, usage = _COMPONENTES["vm"]
    traducao = translate_config("vm", config, usage, quantity=1)

    primeiro = await calculadora.add_line_item("vm", traducao.ui_config)
    segundo = await calculadora.add_line_item("vm", traducao.ui_config)
    assert primeiro != segundo

    await calculadora.edit_line_item(primeiro, {"count": 7})

    lido_primeiro = await (
        calculadora._item_root(primeiro).locator('input[name="count"]').input_value()
    )
    lido_segundo = await (
        calculadora._item_root(segundo).locator('input[name="count"]').input_value()
    )
    assert lido_primeiro == "7"
    assert lido_segundo == "1", "a edição vazou para o outro item"


async def test_osBillingOption_so_existe_para_windows(calculadora):
    """Grupo condicional — trava a descoberta de 21/08.

    O Azure Hybrid Benefit do SO só se aplica a Windows: com Linux, o grupo
    inteiro some do DOM. Este teste existe para que, se a calculadora passar a
    montá-lo sempre (ou nunca), a gente descubra aqui e não numa demo.
    """
    item_id = await calculadora.add_line_item(
        "vm", {"region": "East US", "operatingSystem": "Linux"}
    )
    root = calculadora._item_root(item_id)
    grupo = 'input[id$="-osBillingOption"]'

    assert await root.locator(grupo).count() == 0, "com Linux o grupo não existe"

    await calculadora.edit_line_item(item_id, {"operatingSystem": "Windows"})
    await calculadora._page.wait_for_timeout(1500)
    assert await root.locator(grupo).count() > 0, "com Windows o grupo tem que existir"


async def test_sql_tem_exatamente_duas_opcoes_de_licenca(calculadora):
    """O grupo de licença do SQL não tem savings plan (só o de compute tem).

    Havia um mapeamento "savings_1yr" -> "radio-sv-one-year-" que não existe no
    DOM; pedir essa chave morria num timeout de 30s. Removido em 21/08.
    """
    config, usage = _COMPONENTES["sql"]
    traducao = translate_config("sql", config, usage)
    item_id = await calculadora.add_line_item("sql", traducao.ui_config)
    root = calculadora._item_root(item_id)

    ids = await root.evaluate(
        """el => [...el.querySelectorAll('input[id*="softwareBillingOption"]')]"""
        """.map(r => r.id)"""
    )
    assert len(ids) == 2, f"esperado payg + ahb, veio: {ids}"
    for prefixo in _SQL_SOFTWARE_BILLING_PREFIXES.values():
        casaram = [i for i in ids if i.startswith(prefixo)]
        assert len(casaram) == 1, f"prefixo {prefixo!r} não casou 1:1 em {ids}"
