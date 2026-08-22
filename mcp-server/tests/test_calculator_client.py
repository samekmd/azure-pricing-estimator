"""Testes offline do AzureCalculatorClient (sem navegador, sem rede).

O módulo `calculator_client.py` era o único do repo sem cobertura — e é o mais
frágil, porque depende de seletores de DOM de terceiro. Estes testes usam a
`FakePage` (ver fake_page.py) para travar a lógica que NÃO depende do DOM real:
validação de campos, ordem de aplicação, escopo por item, e as regras de erro.

A contraparte — "os seletores mapeados existem mesmo na página de verdade" — é
o teste marcado `browser`, em test_calculator_selectors.py.
"""

from __future__ import annotations

import pytest

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from azure_estimator_mcp.azure.calculator_client import (
    _ACCOUNT_WIDGET,
    AzureCalculatorClient,
    CalculatorAuthError,
)

from .fake_page import FakePage


@pytest.fixture
def client() -> AzureCalculatorClient:
    """Cliente com uma página falsa já 'aberta' (como após create_estimate)."""
    c = AzureCalculatorClient(storage_state_path="/inexistente.json")
    c._page = FakePage()
    return c


def _page(client: AzureCalculatorClient) -> FakePage:
    return client._page  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Pré-condições: chamar fora de ordem falha claro, não com AttributeError.
# --------------------------------------------------------------------------- #


async def test_add_line_item_sem_create_estimate_falha_claro():
    c = AzureCalculatorClient(storage_state_path="/inexistente.json")
    with pytest.raises(RuntimeError, match="create_estimate"):
        await c.add_line_item("vm", {})


async def test_export_estimate_sem_create_estimate_falha_claro():
    c = AzureCalculatorClient(storage_state_path="/inexistente.json")
    with pytest.raises(RuntimeError, match="create_estimate"):
        await c.export_estimate()


async def test_is_authenticated_sem_contexto_falha_claro():
    c = AzureCalculatorClient(storage_state_path="/inexistente.json")
    with pytest.raises(RuntimeError, match="context manager"):
        await c.is_authenticated()


# --------------------------------------------------------------------------- #
# Regra "não adivinhar" (a mesma dos resolvers em meters.py).
# --------------------------------------------------------------------------- #


async def test_servico_sem_picker_mapeado_levanta_value_error(client):
    with pytest.raises(ValueError, match="aks"):
        await client.add_line_item("aks", {"region": "East US"})


@pytest.mark.parametrize(
    ("service", "campo"),
    [
        ("vm", "gpuCount"),
        ("storage", "operatingSystem"),  # existe na VM, não no storage
        ("sql", "count"),  # existe na VM e no storage, não no SQL
    ],
)
async def test_campo_sem_seletor_levanta_not_implemented(client, service, campo):
    """Campo desconhecido PARA, em vez de ser descartado em silêncio."""
    with pytest.raises(NotImplementedError, match=campo):
        await client.add_line_item(service, {"region": "East US", campo: "x"})


async def test_billing_com_chave_desconhecida_levanta_value_error(client):
    with pytest.raises(ValueError, match="reserved_5yr"):
        await client.add_line_item(
            "vm", {"region": "East US", "computeBillingOption": "reserved_5yr"}
        )


async def test_billing_desabilitado_pela_calculadora_falha_claro(client):
    """A calculadora desabilita combinações (ex.: 1yr reserved p/ certos SKUs).

    Tem que virar erro explicativo, não timeout do Playwright tentando clicar
    num elemento desabilitado.
    """
    page = _page(client)
    # Descobre a cadeia do radio aplicando uma vez com ele habilitado.
    await client.add_line_item(
        "vm", {"region": "East US", "computeBillingOption": "reserved_1yr"}
    )
    alvo = [op[1] for op in page.ops if op[0] == "click" and "radio-one-year-" in op[1]]
    assert alvo, "o radio de billing deveria ter sido clicado"

    client._page = FakePage()
    _page(client).enabled[alvo[0]] = False
    with pytest.raises(ValueError, match="desabilitada"):
        await client.add_line_item(
            "vm", {"region": "East US", "computeBillingOption": "reserved_1yr"}
        )


# --------------------------------------------------------------------------- #
# O que é aplicado, e em que ordem.
# --------------------------------------------------------------------------- #


async def test_campos_de_vm_sao_aplicados_com_os_valores_dados(client):
    page = _page(client)
    await client.add_line_item(
        "vm",
        {
            "region": "East US",
            "operatingSystem": "Linux",
            "size": "D2s v3",
            "count": 2,
            "hours": 730,
        },
    )
    valores = page.values_by_field()
    assert valores['select[name="region"]'] == "East US"
    assert valores['select[name="operatingSystem"]'] == "Linux"
    assert valores['input[name="count"]'] == "2"
    assert valores['input[name="hours"]'] == "730"


async def test_storage_aplica_a_unidade_antes_da_capacidade(client):
    """Ordem importa: 500 lido como TB em vez de GB muda o custo em 1000x.

    O tradutor emite storageUnits antes de count (config_translate.py); este
    teste trava que o cliente preserva essa ordem em vez de reordenar o dict.
    """
    page = _page(client)
    await client.add_line_item(
        "storage",
        {
            "region": "East US",
            "storageAccountType": "Standard",
            "accessTier": "Hot",
            "storageUnits": "GB",
            "count": 500,
        },
    )
    campos = page.fields_in_order()
    assert campos.index('select[name="storageUnits"]') < campos.index(
        'input[name="count"]'
    )


async def test_sql_aplica_os_dois_radios_de_billing(client):
    """Os defaults do SQL precificam ~53% abaixo do on-demand se ficarem soltos."""
    page = _page(client)
    await client.add_line_item(
        "sql",
        {
            "region": "East US",
            "vcoreTier": "General Purpose",
            "databaseBillingOption": "payg",
            "softwareBillingOption": "license_included",
        },
    )
    clicados = [op[1] for op in page.ops if op[0] == "click"]
    assert any("-databaseBillingOption" in c for c in clicados)
    assert any("-softwareBillingOption" in c for c in clicados)


# --------------------------------------------------------------------------- #
# Export.
# --------------------------------------------------------------------------- #


async def test_export_estimate_devolve_o_link_do_textarea(client):
    page = _page(client)
    page.counts["#user-display"] = 1  # sessão válida
    chain = '.share-modal[role="dialog"] >> textarea[name="link"]'
    page.values[chain] = "https://azure.com/e/abc123"

    link = await client.export_estimate()
    assert link == "https://azure.com/e/abc123"


# --------------------------------------------------------------------------- #
# Endereçar um item específico (o que o `.last` impedia).
# --------------------------------------------------------------------------- #


async def test_add_line_item_devolve_o_id_do_item(client):
    item_id = await client.add_line_item("vm", {"region": "East US"})
    assert item_id.startswith("virtual-machines-")
    assert item_id.endswith("-layout")


async def test_cada_item_tem_seu_proprio_container(client):
    """Duas VMs (o caso web-tier + app-tier) recebem ids distintos."""
    primeiro = await client.add_line_item("vm", {"region": "East US"})
    segundo = await client.add_line_item("vm", {"region": "West US"})
    assert primeiro != segundo


async def test_todos_os_campos_sao_aplicados_dentro_do_container(client):
    """Nenhum campo escapa para o escopo global — é o que quebrava com `.last`."""
    page = _page(client)
    item_id = await client.add_line_item(
        "vm",
        {"region": "East US", "size": "D2s v3", "count": 2, "hours": 730},
    )
    escopo = f'div[id="{item_id}"] >> '
    aplicados = [op for op in page.applied()]
    assert aplicados, "nada foi aplicado"
    for op in aplicados:
        assert op[1].startswith(escopo), f"campo fora do container: {op[1]}"


async def test_size_do_segundo_item_nao_vaza_para_o_primeiro(client):
    """Com 2 VMs o documento tem DOIS id='size' (id duplicado, real).

    Antes, `#size.last` sempre pegava o último — o motivo de não dar para
    editar um item já adicionado.
    """
    page = _page(client)
    primeiro = await client.add_line_item("vm", {"region": "East US"})
    page.ops.clear()
    segundo = await client.add_line_item("vm", {"size": "D4s v3"})

    alvos = [op[1] for op in page.ops if "#size" in op[1]]
    assert alvos, "o combobox de instância deveria ter sido tocado"
    assert all(a.startswith(f'div[id="{segundo}"]') for a in alvos)
    assert not any(primeiro in a for a in alvos)


async def test_edit_line_item_mira_o_item_pedido(client):
    """Editar o PRIMEIRO item com um segundo já na lista."""
    page = _page(client)
    primeiro = await client.add_line_item("vm", {"region": "East US"})
    await client.add_line_item("vm", {"region": "West US"})
    page.ops.clear()

    await client.edit_line_item(primeiro, {"count": 5})
    aplicados = page.applied()
    assert aplicados
    for op in aplicados:
        assert op[1].startswith(f'div[id="{primeiro}"] >> ')
    assert page.values_by_field()['input[name="count"]'] == "5"


async def test_edit_line_item_deduz_o_servico_do_id(client):
    """Campo de VM num item de storage é recusado, sem o chamador dizer o tipo."""
    item_id = await client.add_line_item("storage", {"region": "East US"})
    with pytest.raises(NotImplementedError, match="operatingSystem"):
        await client.edit_line_item(item_id, {"operatingSystem": "Linux"})


async def test_edit_line_item_com_id_malformado_falha_claro(client):
    with pytest.raises(ValueError, match="formato"):
        await client.edit_line_item("item-3", {"count": 1})


async def test_edit_line_item_de_item_ausente_falha_claro(client):
    """Id válido mas de outra estimativa (ex.: create_estimate foi refeito)."""
    await client.add_line_item("vm", {"region": "East US"})
    outro = "virtual-machines-99999999-0000-0000-0000-000000000000-layout"
    with pytest.raises(ValueError, match="não está na estimativa"):
        await client.edit_line_item(outro, {"count": 1})


async def test_edit_line_item_sem_create_estimate_falha_claro():
    c = AzureCalculatorClient(storage_state_path="/inexistente.json")
    with pytest.raises(RuntimeError, match="create_estimate"):
        await c.edit_line_item("virtual-machines-x-layout", {})


# --------------------------------------------------------------------------- #
# Export: falha limpa (o item "Share" NÃO desabilita quando deslogado).
# --------------------------------------------------------------------------- #


async def test_export_deslogado_levanta_erro_de_sessao(client):
    page = _page(client)
    page.counts[_ACCOUNT_WIDGET] = 0  # deslogado: o widget some do DOM

    with pytest.raises(CalculatorAuthError, match="bootstrap_login"):
        await client.export_estimate()


async def test_export_deslogado_nem_chega_a_clicar_em_share(client):
    """Falha ANTES de mexer na UI — senão morre num timeout opaco de 10s."""
    page = _page(client)
    page.counts[_ACCOUNT_WIDGET] = 0

    with pytest.raises(CalculatorAuthError):
        await client.export_estimate()
    assert page.ops == [], f"não deveria ter tocado a página: {page.ops}"


async def test_export_com_dialogo_que_nao_abre_vira_erro_explicativo(client):
    """Sessão válida mas o dialog não abre (layout mudou) -> erro claro."""
    page = _page(client)
    page.counts[_ACCOUNT_WIDGET] = 1
    chain = '.share-modal[role="dialog"] >> textarea[name="link"]'
    page.scripted_errors[chain] = (
        "wait_for",
        PlaywrightTimeoutError("Timeout 10000ms exceeded"),
    )

    with pytest.raises(RuntimeError, match="seletores do menu Share"):
        await client.export_estimate()


async def test_grupo_de_billing_ausente_falha_claro(client):
    """Grupo condicional inexistente (ex.: osBillingOption com Linux).

    Sem esta checagem, o locator não casa com nada e o Playwright fica 30s
    tentando antes de estourar um timeout que não explica nada.
    """
    page = _page(client)
    item_id = await client.add_line_item("vm", {"region": "East US"})
    grupo = (
        f'div[id="{item_id}"] >> '
        'input[id^="radio-ahb-"][id$="-osBillingOption"]'
    )
    page.counts[grupo] = 0

    with pytest.raises(ValueError, match="não existe no painel"):
        await client.edit_line_item(item_id, {"osBillingOption": "azure_hybrid_benefit"})


async def test_licenca_de_sql_nao_oferece_savings_plan(client):
    """Só payg e AHB — o grupo de licença do SQL não tem savings plan."""
    with pytest.raises(ValueError, match="savings_1yr"):
        await client.add_line_item(
            "sql", {"region": "East US", "softwareBillingOption": "savings_1yr"}
        )
