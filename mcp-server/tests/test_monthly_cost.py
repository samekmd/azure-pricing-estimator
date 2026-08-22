"""Testes de cobertura de unidades em monthly_cost (sem rede).

Trava a lacuna das unidades diárias ("1/Day", cobrado por dia: ACR Premium e
afins) sem afrouxar a regra de "unidade desconhecida levanta erro".
"""

import pytest

from azure_estimator_mcp.azure.pricing import DAYS_PER_MONTH, monthly_cost
from azure_estimator_mcp.models import PriceResult


def _price(uom, unit_price=1.0):
    return PriceResult(
        unit_price=unit_price, currency="USD", unit_of_measure=uom,
        meter_id="m", meter_name="n", sku_name="s", region="eastus",
        price_type="Consumption",
    )


@pytest.mark.parametrize("uom", ["1/Day", "1 /Day", "1 Day", "1/day", "1 DAY"])
def test_unidade_diaria_multiplica_por_30(uom):
    """Todas as variações de grafia convertem dia->mês com o fator 30."""
    assert monthly_cost(_price(uom, 1.667), {}) == pytest.approx(1.667 * 30)


def test_acr_premium_bate_com_a_calculadora_oficial():
    # ACR Premium: ~US$ 1,667/dia x 30 = ~US$ 50/mês, o número da calculadora.
    assert monthly_cost(_price("1/Day", 1.667), {}) == pytest.approx(50.0, abs=0.05)


def test_dias_customizados():
    assert monthly_cost(_price("1/Day", 2.0), {"days": 10}) == pytest.approx(20.0)


def test_unidade_diaria_com_fator_numerico():
    # "10/Day" -> o preço é por 10 unidades/dia.
    assert monthly_cost(_price("10/Day", 5.0), {}) == pytest.approx(5.0 * 30 / 10)


def test_dias_por_mes_e_30_fixo():
    """Convenção deliberada: 30, não 365/12 — foi o que casou com a Azure."""
    assert DAYS_PER_MONTH == 30


@pytest.mark.parametrize("uom", ["1/Fortnight", "1 Transaction", "1 Nonsense", "1/Year"])
def test_unidade_desconhecida_continua_levantando(uom):
    with pytest.raises(ValueError, match="não reconhecido"):
        monthly_cost(_price(uom), {})


def test_unidades_ja_cobertas_nao_regridem():
    assert monthly_cost(_price("1 Hour", 0.1), {}) == pytest.approx(73.0)
    assert monthly_cost(_price("1 Hour", 0.1), {"hours": 100}) == pytest.approx(10.0)
    assert monthly_cost(_price("1 GB/Month", 0.02), {"gb": 500}) == pytest.approx(10.0)
    assert monthly_cost(_price("100 GB/Month", 5.0), {"gb": 300}) == pytest.approx(15.0)
    assert monthly_cost(_price("1/Month", 4.2), {}) == pytest.approx(4.2)
    with pytest.raises(ValueError):
        monthly_cost(_price("1 GB/Month"), {})


# --------------------------------------------------------------------------- #
# TB processado — a unidade que o Synapse serverless trouxe (21/08).
#
# Primeiro eixo de cobrança do projeto que não é tempo nem capacidade
# armazenada: o custo acompanha o volume CONSULTADO.
# --------------------------------------------------------------------------- #
def test_tb_processado_usa_o_volume_consultado():
    # Synapse serverless SQL pool em eastus: $5/TB. 5 TB no mês = $25.
    assert monthly_cost(_price("1 TB", 5.0), {"tbProcessed": 5}) == pytest.approx(25.0)


def test_tb_aceita_o_alias_curto():
    """'tbProcessed' é o nome nos padrões; 'tb' mantém a simetria com 'gb'."""
    assert monthly_cost(_price("1 TB", 5.0), {"tb": 5}) == pytest.approx(25.0)


def test_tb_respeita_o_fator_numerico_da_unidade():
    assert monthly_cost(_price("10 TB", 50.0), {"tbProcessed": 5}) == pytest.approx(25.0)


def test_tb_sem_volume_levanta_em_vez_de_assumir_um_default():
    """Não existe volume consultado 'padrão'.

    Diferente de horas, onde 730 é o mês cheio e é uma convenção defensável,
    aqui um default seria inventar a conta inteira — um número plausível e
    errado é pior que um erro claro.
    """
    with pytest.raises(ValueError, match="tbProcessed"):
        monthly_cost(_price("1 TB", 5.0), {})


def test_tb_nao_se_confunde_com_gb():
    """usage['gb'] não satisfaz um meter cobrado em TB."""
    with pytest.raises(ValueError, match="tbProcessed"):
        monthly_cost(_price("1 TB", 5.0), {"gb": 5000})
