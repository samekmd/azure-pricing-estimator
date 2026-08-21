"""Testes do tradutor config de resolver -> config da UI da calculadora.

Função pura (dict -> dict): roda offline, sem navegador e sem rede.

O foco é o que quebra em silêncio. Os rótulos da UI foram sondados do DOM real
e são batidos por texto exato pelo select_option(label=...): um rótulo errado
não dá erro de tradução, dá timeout do Playwright lá na frente. Por isso os
testes travam os rótulos, não só o formato.
"""

from __future__ import annotations

import pytest

from azure_estimator_mcp.azure.config_translate import (
    _TRANSLATORS,
    ConfigTranslationError,
    translate_config,
)

# Reaproveita o carregador dos padrões do validador pattern-driven.
from .test_patterns_resolve import ALL_COMPONENTS, BLOQUEADOS, PATTERNS_DIR  # noqa: F401

# Componentes resolvíveis dos padrões — definido cedo porque vários testes
# abaixo parametrizam em cima dele.
RESOLVIVEIS = [(cid, c) for cid, c in ALL_COMPONENTS if c["service"] not in BLOQUEADOS]

# As duas metades do projeto não avançam no mesmo passo, e este arquivo é sobre
# a metade da CALCULADORA. Um serviço pode já resolver PREÇO (meters.py) e
# ainda não ter tradução para a UI — é o caso do 'aks' desde 21/08, cujo
# resolver entrou pela trilha de preço enquanto o painel da calculadora ainda
# não foi mapeado. Derivar a lista de _TRANSLATORS (em vez de repetir nomes à
# mão) mantém a distinção honesta sozinha: quando a tradução do aks entrar, ele
# migra para TRADUZIVEIS e passa a ser exercitado por todos os testes abaixo.
TRADUZIVEIS = [(cid, c) for cid, c in RESOLVIVEIS if c["service"] in _TRANSLATORS]
SEM_TRADUCAO = [(cid, c) for cid, c in RESOLVIVEIS if c["service"] not in _TRANSLATORS]


# --------------------------------------------------------------------------- #
# Os dois defaults da UI que contradizem os resolvers — se o tradutor deixar de
# emitir esses campos, a calculadora precifica OUTRO produto sem erro nenhum.
# São os testes mais importantes deste arquivo.
# --------------------------------------------------------------------------- #
def test_vm_sempre_emite_os_mesmo_para_linux():
    """UI tem default Windows; resolver tem default Linux. Omitir = VM Windows."""
    t = translate_config("vm", {"armSkuName": "Standard_D2s_v3", "region": "East US"})
    assert t.ui_config["operatingSystem"] == "Linux"


def test_vm_windows_explicito():
    t = translate_config(
        "vm", {"armSkuName": "Standard_D2s_v3", "region": "East US", "windows": True}
    )
    assert t.ui_config["operatingSystem"] == "Windows"


def test_sql_sempre_emite_vcore_tier():
    """UI tem default Hyperscale; resolver tem default General Purpose."""
    t = translate_config("sql", {"region": "East US", "vCores": 2})
    assert t.ui_config["vcoreTier"] == "General Purpose"


def test_sql_gen5_vira_rotulo_da_ui():
    """'Gen5' (Retail API) != 'Standard-series (Gen 5)' (calculadora)."""
    t = translate_config(
        "sql", {"region": "East US", "hardware": "Gen5", "vCores": 2}
    )
    assert t.ui_config["generation"] == "Standard-series (Gen 5)"


# --------------------------------------------------------------------------- #
# Ordem de aplicação: add_line_item itera config.items() na ordem de inserção,
# e trocar um select recarrega as opções dos de baixo.
# --------------------------------------------------------------------------- #
def test_sql_ordem_grosso_para_fino():
    campos = list(translate_config("sql", {"region": "East US", "vCores": 2}).ui_config)
    assert campos.index("vcoreTier") < campos.index("generation")
    assert campos.index("generation") < campos.index("instanceSize")
    assert campos.index("purchaseModel") < campos.index("vcoreTier")


def test_storage_ordem_conta_antes_de_tier():
    campos = list(translate_config("storage", {"region": "East US"}).ui_config)
    assert campos.index("storageAccountType") < campos.index("accessTier")
    assert campos.index("accessTier") < campos.index("redundancy")


# --------------------------------------------------------------------------- #
# Conversões
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("arm_sku", "esperado"),
    [
        ("Standard_D2s_v3", "D2s v3"),
        ("Standard_D4s_v3", "D4s v3"),
        ("Standard_E4ds_v5", "E4ds v5"),
        ("D2s_v3", "D2s v3"),  # já sem prefixo
    ],
)
def test_arm_sku_vira_texto_de_busca(arm_sku, esperado):
    t = translate_config("vm", {"armSkuName": arm_sku, "region": "East US"})
    assert t.ui_config["size"] == esperado


def test_regiao_slug_vira_rotulo_amigavel():
    """A UI casa o <option> por texto: 'eastus' não existe lá, 'East US' sim."""
    t = translate_config("vm", {"armSkuName": "Standard_D2s_v3", "region": "eastus"})
    assert t.ui_config["region"] == "East US"


def test_regiao_amigavel_passa_direto():
    t = translate_config("vm", {"armSkuName": "Standard_D2s_v3", "region": "West Europe"})
    assert t.ui_config["region"] == "West Europe"


def test_regiao_slug_desconhecido_para_com_erro_claro():
    """Slug fora do mapa não é chutado — viraria timeout no select_option."""
    with pytest.raises(ConfigTranslationError, match="slug"):
        translate_config("vm", {"armSkuName": "Standard_D2s_v3", "region": "japaneast"})


def test_vm_usa_quantity_e_hours():
    t = translate_config(
        "vm",
        {"armSkuName": "Standard_D2s_v3", "region": "East US"},
        usage={"hours": 200},
        quantity=3,
    )
    assert t.ui_config["count"] == 3
    assert t.ui_config["hours"] == 200


def test_vm_hours_default_730():
    t = translate_config("vm", {"armSkuName": "Standard_D2s_v3", "region": "East US"})
    assert t.ui_config["hours"] == 730


# --------------------------------------------------------------------------- #
# Não adivinhar (mesma regra dos resolvers em meters.py)
# --------------------------------------------------------------------------- #
def test_servico_sem_tradutor_para():
    with pytest.raises(ConfigTranslationError, match="aks"):
        translate_config("aks", {"region": "East US"})


def test_price_type_sem_mapeamento_para():
    with pytest.raises(ConfigTranslationError, match="billing"):
        translate_config(
            "vm",
            {"armSkuName": "Standard_D2s_v3", "region": "East US",
             "priceType": "Reservation"},
        )


def test_sql_sem_vcores_para():
    with pytest.raises(ConfigTranslationError, match="vCores"):
        translate_config("sql", {"region": "East US"})


def test_sql_vcores_invalido_para():
    with pytest.raises(ConfigTranslationError, match="instanceSize"):
        translate_config("sql", {"region": "East US", "vCores": 3})


def test_storage_redundancy_invalida_para():
    with pytest.raises(ConfigTranslationError, match="redundancy"):
        translate_config("storage", {"region": "East US", "redundancy": "XPTO"})


def test_sem_regiao_para():
    with pytest.raises(ConfigTranslationError, match="region"):
        translate_config("vm", {"armSkuName": "Standard_D2s_v3"})


# --------------------------------------------------------------------------- #
# O que a UI NÃO consegue receber tem que ser declarado, não sumir em silêncio.
# --------------------------------------------------------------------------- #
def test_storage_aplica_capacidade_do_usage():
    """usage['gb'] vira a capacidade do painel (campo 'count', em GB).

    Sem isso a calculadora usa o default dela — 1.000 GB — e o link diverge do
    custo que o nosso núcleo calculou, sem nada explicando a diferença.
    """
    t = translate_config("storage", {"region": "East US"}, usage={"gb": 500})
    assert t.ui_config["count"] == 500
    assert t.ui_config["storageUnits"] == "GB"
    # Não pode mais ser declarado como não aplicado: agora ele é aplicado.
    assert not any("gb" in u for u in t.unsupported)


def test_storage_sem_gb_declara_default():
    """Sem volume informado, o default de 1.000 GB tem que ficar VISÍVEL."""
    t = translate_config("storage", {"region": "East US"})
    assert "count" not in t.ui_config
    assert any("1.000 GB" in u for u in t.unsupported)


def test_storage_capacidade_vem_depois_da_unidade():
    """A unidade tem que ser aplicada antes do número, senão lê-se em TB."""
    campos = list(
        translate_config("storage", {"region": "East US"}, usage={"gb": 500}).ui_config
    )
    assert campos.index("storageUnits") < campos.index("count")


def test_storage_sempre_emite_billing_payg():
    """O default aqui já é Pay as you go — mas default não é escolha."""
    t = translate_config("storage", {"region": "East US"}, usage={"gb": 500})
    assert t.ui_config["blobBillingOption"] == "payg"


def test_storage_price_type_nao_consumption_para():
    with pytest.raises(ConfigTranslationError, match="billing"):
        translate_config(
            "storage", {"region": "East US", "priceType": "Reservation"},
            usage={"gb": 500},
        )


def test_storage_declara_operacoes_no_default():
    """Os contadores de operações são cobrados à parte e não vêm da config."""
    t = translate_config("storage", {"region": "East US"}, usage={"gb": 500})
    assert any("OPERA" in a.upper() for a in t.assumptions)


@pytest.mark.parametrize(
    ("caso_id", "comp"),
    [pytest.param(cid, c, id=cid) for cid, c in TRADUZIVEIS if c["service"] == "storage"],
)
def test_storage_do_padrao_leva_o_volume(caso_id, comp):
    """Os 3 storages dos padrões (200/500/1000 GB) chegam à UI com o volume certo."""
    t = translate_config("storage", comp["config"], comp["usage"])
    assert t.ui_config["count"] == comp["usage"]["gb"]


def test_vm_declara_premissas():
    t = translate_config("vm", {"armSkuName": "Standard_D2s_v3", "region": "East US"})
    assert t.assumptions  # type/tier/category ficaram no default
    assert any("Ubuntu" in a for a in t.assumptions)


# --------------------------------------------------------------------------- #
# Integração com os padrões da Skill: TODO componente resolvível traduz.
# É o que fecha a fatia padrão -> link.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("caso_id", "comp"), [pytest.param(cid, c, id=cid) for cid, c in TRADUZIVEIS]
)
def test_componente_do_padrao_traduz(caso_id, comp):
    t = translate_config(
        comp["service"], comp["config"], comp.get("usage") or {},
        comp.get("quantity", 1),
    )
    assert t.ui_config["region"] == "East US"
    # Nenhum valor traduzido pode vir vazio: rótulo vazio vira erro só na UI.
    assert all(v != "" and v is not None for v in t.ui_config.values())


@pytest.mark.parametrize(
    ("caso_id", "comp"),
    [pytest.param(cid, c, id=cid) for cid, c in TRADUZIVEIS if c["service"] == "vm"],
)
def test_vm_do_padrao_vai_como_linux(caso_id, comp):
    """Todos os componentes de VM dos padrões são Linux — nenhum pode virar Windows."""
    t = translate_config("vm", comp["config"], comp.get("usage") or {})
    assert t.ui_config["operatingSystem"] == "Linux"


@pytest.mark.parametrize(
    ("caso_id", "comp"), [pytest.param(cid, c, id=cid) for cid, c in SEM_TRADUCAO]
)
def test_componente_que_resolve_preco_mas_nao_traduz_falha_claro(caso_id, comp):
    """Documenta um buraco REAL da fatia vertical, em vez de escondê-lo.

    Estes componentes têm preço (resolve_price funciona) mas não podem ser
    adicionados à calculadora: o painel do serviço ainda não foi mapeado. O
    erro é limpo — ConfigTranslationError, não um timeout do Playwright — e a
    Skill consegue dizer ao usuário "sei o custo, não sei montar o item".

    Quando a tradução entrar, a lista SEM_TRADUCAO esvazia e este teste some
    junto (parametrização vazia), sem ninguém precisar lembrar de removê-lo.
    """
    with pytest.raises(ConfigTranslationError, match="não tem tradução"):
        translate_config(comp["service"], comp["config"], comp.get("usage") or {})


def test_componentes_bloqueados_nao_traduzem():
    for cid, comp in ALL_COMPONENTS:
        if comp["service"] in BLOQUEADOS:
            with pytest.raises(ConfigTranslationError):
                translate_config(comp["service"], comp["config"])


# --------------------------------------------------------------------------- #
# Billing do SQL — os dois defaults mais perigosos da calculadora inteira.
# Medidos ao vivo: o painel vem em "3 year reserved (~55% discount)" +
# "Bring Your Own License (Azure Hybrid Benefit)". Um item deixado assim sai
# ~53% abaixo do on-demand que a Retail Prices API cobra pela MESMA config, e
# nada na tela avisa. Se estes testes caírem, a estimativa volta a mentir.
# --------------------------------------------------------------------------- #
def test_sql_sempre_emite_billing_payg():
    t = translate_config("sql", {"region": "East US", "vCores": 2})
    assert t.ui_config["databaseBillingOption"] == "payg"


def test_sql_sempre_emite_licenca_inclusa():
    t = translate_config("sql", {"region": "East US", "vCores": 2})
    assert t.ui_config["softwareBillingOption"] == "license_included"


def test_sql_price_type_nao_consumption_para():
    with pytest.raises(ConfigTranslationError, match="billing"):
        translate_config(
            "sql", {"region": "East US", "vCores": 2, "priceType": "Reservation"}
        )


@pytest.mark.parametrize(
    ("caso_id", "comp"),
    [pytest.param(cid, c, id=cid) for cid, c in TRADUZIVEIS if c["service"] == "sql"],
)
def test_sql_do_padrao_vai_on_demand_com_licenca(caso_id, comp):
    t = translate_config("sql", comp["config"], comp.get("usage") or {})
    assert t.ui_config["databaseBillingOption"] == "payg"
    assert t.ui_config["softwareBillingOption"] == "license_included"
