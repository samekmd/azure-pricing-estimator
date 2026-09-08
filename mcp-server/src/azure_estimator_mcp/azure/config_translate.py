"""Tradução config de resolver -> config da UI da calculadora.

As duas metades do projeto falam vocabulários diferentes para a MESMA coisa:

    resolver/padrão (meters.py)      UI da calculadora (calculator_client.py)
    ---------------------------      ---------------------------------------
    armSkuName: Standard_D2s_v3      size: "D2s v3"
    windows: false                   operatingSystem: "Linux"
    tier: General Purpose            vcoreTier: "General Purpose"
    hardware: Gen5                   generation: "Standard-series (Gen 5)"
    vCores: 2                        instanceSize: "2 vCore"
    usage['gb']: 500                 count: 500 (+ storageUnits: "GB")

Este módulo é a ponte. Ele é PURO (dict -> dict): não importa Playwright nem
fala com a Retail Prices API, então não viola o desacoplamento dos dois
subsistemas — pode ser testado sem rede e sem navegador.

Os rótulos do lado direito são o texto EXATO dos <option>, porque
add_line_item usa select_option(label=...). Todos foram obtidos por sondagem
do DOM real da calculadora em 20/08 (mesma disciplina do _SPECIAL_REGIONS em
retail_client.py: rótulo confirmado, não suposto). Rótulo inventado não falha
com erro claro — falha com timeout do Playwright.

PRINCÍPIO DE SEGURANÇA — emitir o que se sabe, nunca confiar no default da UI.
Quatro defaults da calculadora contradizem o que os resolvers assumem:

    operatingSystem        default da UI = Windows          <-> resolver: Linux
    vcoreTier              default da UI = Hyperscale       <-> resolver: General Purpose
    databaseBillingOption  default da UI = 3 year reserved  <-> resolver: Consumption
    softwareBillingOption  default da UI = Hybrid Benefit   <-> resolver: licença inclusa

Os dois de SQL foram medidos ao vivo e são os piores: um item deixado no
default sai ~53% mais barato (reserva de 3 anos, sem licença) que o meter
on-demand que a Retail Prices API devolve para a MESMA config. Nada na tela
avisa; só o número no fim muda.

Por isso todo campo que a config determina é emitido explicitamente, mesmo
quando por acaso coincide com o default. O que a config NÃO determina vira uma
premissa declarada em `assumptions`, para o chamador mostrar ao usuário.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ConfigTranslationError(Exception):
    """Não foi possível traduzir a config para o vocabulário da UI.

    Mesma regra dos resolvers em meters.py: não adivinhar. Um valor sem
    rótulo conhecido para dele, em vez de mandar um palpite para a UI.
    """


@dataclass
class Translation:
    """Resultado da tradução."""

    ui_config: dict[str, object]
    """Config pronta para AzureCalculatorClient.add_line_item.

    A ORDEM importa: os dicts preservam ordem de inserção e add_line_item
    aplica os campos nessa ordem. Campos grosseiros vêm antes dos finos porque
    mudar um select recarrega as opções dos de baixo (trocar vcoreTier
    remonta a lista de instanceSize, por exemplo). A ordem usada é a mesma em
    que os campos aparecem no painel da calculadora.
    """

    assumptions: list[str] = field(default_factory=list)
    """Premissas assumidas: campos da UI que a config não determina."""

    unsupported: list[str] = field(default_factory=list)
    """O que veio na config/usage e NÃO pôde ser aplicado na UI."""


# --------------------------------------------------------------------------- #
# Região: a UI quer o nome amigável ("East US"), os resolvers aceitam os dois.
# --------------------------------------------------------------------------- #
# Só as regiões que o projeto já trata como conhecidas (espelha o _REGION_MAP
# de retail_client.py). Um slug fora daqui não é chutado: para com erro claro
# pedindo o nome amigável, que é o que o <option> da calculadora usa.
_SLUG_TO_UI_LABEL = {
    "eastus": "East US",
    "eastus2": "East US 2",
    "westus": "West US",
    "westus2": "West US 2",
    "centralus": "Central US",
    "westeurope": "West Europe",
    "northeurope": "North Europe",
    "uksouth": "UK South",
    "southeastasia": "Southeast Asia",
    "brazilsouth": "Brazil South",
}


def _region_to_ui_label(region: str) -> str:
    if not region:
        raise ConfigTranslationError("config['region'] é obrigatório.")
    texto = str(region).strip()
    # Forma amigável ("East US") já é o rótulo do <option>: passa direto.
    if " " in texto or any(c.isupper() for c in texto):
        return texto
    slug = texto.lower()
    if slug in _SLUG_TO_UI_LABEL:
        return _SLUG_TO_UI_LABEL[slug]
    raise ConfigTranslationError(
        f"Região {region!r} está em formato de slug e não está no mapa de "
        f"regiões conhecidas ({sorted(_SLUG_TO_UI_LABEL)}). Passe o nome "
        "amigável usado pela calculadora (ex.: 'East US')."
    )


def _require_consumption(config: dict, servico: str, opcoes: str) -> None:
    """Só on-demand é traduzível hoje; reserva/savings não têm mapeamento."""
    price_type = str(config.get("priceType", "Consumption")).strip().lower()
    if price_type != "consumption":
        raise ConfigTranslationError(
            f"priceType {config.get('priceType')!r} não tem opção de billing "
            f"mapeada para {servico} (a calculadora tem {opcoes}, mas o "
            "mapeamento config->radio ainda não foi definido)."
        )


# --------------------------------------------------------------------------- #
# VM
# --------------------------------------------------------------------------- #
# priceType do resolver -> radio de billing de compute (ver
# _VM_COMPUTE_BILLING_PREFIXES em calculator_client.py).
_PRICE_TYPE_TO_BILLING = {"consumption": "payg"}

# Rótulos confirmados por sondagem (20/08).
_VM_OS_LABELS = {False: "Linux", True: "Windows"}


def _arm_sku_to_size(arm_sku: str) -> str:
    """'Standard_D2s_v3' -> 'D2s v3' (texto de busca do combobox INSTANCE).

    O campo #size é um react-select com busca; add_line_item digita esse texto
    e clica na PRIMEIRA sugestão. Por isso o texto tem que ser específico: o
    nome do SKU sem o prefixo 'Standard_' é o que a calculadora exibe
    ('D2s v3: 2 vCPUs, 8 GB RAM, ...'). Ainda assim, um SKU cujo nome seja
    prefixo de outro pode casar errado — limitação herdada de _apply_vm_size.
    """
    if not arm_sku:
        raise ConfigTranslationError("config['armSkuName'] é obrigatório para 'vm'.")
    nome = str(arm_sku).strip()
    if nome.lower().startswith("standard_"):
        nome = nome[len("standard_"):]
    return nome.replace("_", " ")


def _translate_vm(config: dict, usage: dict, quantity: int) -> Translation:
    ui: dict[str, object] = {}
    assumptions: list[str] = []
    unsupported: list[str] = []

    price_type = str(config.get("priceType", "Consumption")).strip().lower()
    billing = _PRICE_TYPE_TO_BILLING.get(price_type)
    if billing is None:
        raise ConfigTranslationError(
            f"priceType {config.get('priceType')!r} não tem opção de billing "
            f"mapeada na UI (conhecidos: {sorted(_PRICE_TYPE_TO_BILLING)}). "
            "Reservas/savings plans existem na calculadora, mas o mapeamento "
            "config->radio ainda não foi definido."
        )

    ui["region"] = _region_to_ui_label(config["region"])
    # SEMPRE emitido: o default da UI é Windows, o do resolver é Linux.
    ui["operatingSystem"] = _VM_OS_LABELS[bool(config.get("windows", False))]
    ui["size"] = _arm_sku_to_size(config.get("armSkuName", ""))
    ui["count"] = int(quantity)
    ui["hours"] = int(usage.get("hours", 730))
    ui["computeBillingOption"] = billing

    assumptions.append(
        "VM: 'type' (=(OS Only)), 'tier' (=Standard) e 'category' (=All) ficaram "
        "no default da calculadora — a config do resolver não tem equivalente."
    )
    if not config.get("windows", False):
        assumptions.append(
            "VM Linux: a distribuição fica no default da calculadora (Ubuntu); "
            "o resolver só distingue Linux vs Windows."
        )
    for extra in sorted(set(usage) - {"hours"}):
        unsupported.append(f"usage[{extra!r}] não se aplica a uma VM na UI.")

    return Translation(ui, assumptions, unsupported)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
# productName da Retail API -> storageAccountType da UI. "Blob Storage" é a
# conta Blob clássica; "General Block Blob v2" é a GPv2 (nomes diferentes para
# a mesma distinção dos dois lados).
_STORAGE_PRODUCT_TO_ACCOUNT_TYPE = {
    "blob storage": "Blob Storage",
    "general block blob v2": "General Purpose V2",
}
_STORAGE_ACCESS_TIERS = {"hot": "Hot", "cool": "Cool", "cold": "Cold",
                         "archive": "Archive"}
_STORAGE_REDUNDANCIES = {r.lower(): r for r in
                         ("LRS", "ZRS", "GRS", "RA-GRS", "GZRS", "RA-GZRS")}


def _translate_storage(config: dict, usage: dict, quantity: int) -> Translation:
    ui: dict[str, object] = {}
    assumptions: list[str] = []
    unsupported: list[str] = []

    tier = str(config.get("tier", "Hot")).strip().lower()
    if tier not in _STORAGE_ACCESS_TIERS:
        raise ConfigTranslationError(
            f"tier {config.get('tier')!r} não é um accessTier da calculadora "
            f"(conhecidos: {sorted(_STORAGE_ACCESS_TIERS.values())})."
        )
    redundancy = str(config.get("redundancy", "LRS")).strip().lower()
    if redundancy not in _STORAGE_REDUNDANCIES:
        raise ConfigTranslationError(
            f"redundancy {config.get('redundancy')!r} não é uma opção da "
            f"calculadora (conhecidas: {sorted(_STORAGE_REDUNDANCIES.values())})."
        )
    product = str(config.get("productName", "Blob Storage")).strip().lower()
    account_type = _STORAGE_PRODUCT_TO_ACCOUNT_TYPE.get(product)
    if account_type is None:
        raise ConfigTranslationError(
            f"productName {config.get('productName')!r} não tem tipo de conta "
            f"equivalente na UI (conhecidos: "
            f"{sorted(_STORAGE_PRODUCT_TO_ACCOUNT_TYPE)})."
        )
    _require_consumption(config, "storage", "reservas de 1 e 3 anos")

    # Ordem = ordem do painel: type e storageAccountType antes de accessTier,
    # porque trocar o tipo de conta remonta as opções de tier/redundância; a
    # unidade vem antes da capacidade, para o número ser lido na unidade certa.
    ui["region"] = _region_to_ui_label(config["region"])
    ui["type"] = "Block Blob Storage"
    ui["storageAccountType"] = account_type
    ui["accessTier"] = _STORAGE_ACCESS_TIERS[tier]
    ui["redundancy"] = _STORAGE_REDUNDANCIES[redundancy]

    gb = usage.get("gb")
    if gb is not None:
        # "count" no painel de storage é a CAPACIDADE, não a quantidade de
        # contas (na VM o mesmo name significa nº de instâncias). Sem isso a
        # calculadora usa o default dela — 1.000 GB — e o link diverge do custo
        # que o nosso núcleo calculou, sem nada explicando a diferença.
        ui["storageUnits"] = "GB"
        ui["count"] = int(gb)
    # Emitido mesmo já sendo o default da UI: default não é escolha.
    ui["blobBillingOption"] = "payg"

    assumptions.append(
        "Storage: 'performanceTier' (=Standard) e 'fileStructure' "
        "(=Flat Namespace) ficaram no default — a config do resolver não os "
        "distingue."
    )
    assumptions.append(
        "Storage: os contadores de OPERAÇÕES (write/read/create container/"
        "outras) e o volume de recuperação de dados ficaram no default da "
        "calculadora, que os cobra à parte. O nosso núcleo precifica só o "
        "meter de capacidade ('Data Stored'), então o total da calculadora "
        "fica um pouco acima por causa dessas linhas."
    )
    if gb is None:
        unsupported.append(
            "usage['gb'] não informado: a capacidade ficou no default da "
            "calculadora (1.000 GB)."
        )
    if quantity != 1:
        unsupported.append(
            f"quantity={quantity}: o painel de storage não tem campo de "
            "quantidade de contas; a capacidade já é o 'count'. Adicione o "
            "item mais de uma vez se precisar de contas separadas."
        )
    return Translation(ui, assumptions, unsupported)


# --------------------------------------------------------------------------- #
# SQL Database
# --------------------------------------------------------------------------- #
_SQL_TIERS = {t.lower(): t for t in
              ("General Purpose", "Business Critical", "Hyperscale")}
_SQL_COMPUTE = {"provisioned": "Provisioned", "serverless": "Serverless"}
# A família de hardware tem nomes BEM diferentes dos dois lados: a Retail API
# diz "Gen5", a calculadora diz "Standard-series (Gen 5)". Confirmado por
# sondagem — é o mapeamento que mais convida ao erro.
#
# Nota: a lista de gerações OFERECIDAS muda com o vcoreTier (General Purpose
# oferece Standard-series (Gen 5)/Fsv2-series/DC-series; Hyperscale oferece
# Premium-series e a variante memory optimized). Este mapa é a união das duas;
# pedir uma geração que não existe no tier escolhido falha na UI, não aqui.
_SQL_GENERATIONS = {
    "gen5": "Standard-series (Gen 5)",
    "standard-series (gen 5)": "Standard-series (Gen 5)",
    "dc-series": "DC-series",
    "fsv2": "Fsv2-series",
    "fsv2-series": "Fsv2-series",
    "premium-series": "Premium-series",
    "premium-series, memory optimized": "Premium-series, memory optimized",
}
# vCores oferecidos pelo <select instanceSize> (sondados).
_SQL_VCORES = (2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 32, 40, 80)


def _translate_sql(config: dict, usage: dict, quantity: int) -> Translation:
    ui: dict[str, object] = {}
    assumptions: list[str] = []
    unsupported: list[str] = []

    tier = str(config.get("tier", "General Purpose")).strip().lower()
    if tier not in _SQL_TIERS:
        raise ConfigTranslationError(
            f"tier {config.get('tier')!r} não é um vcoreTier da calculadora "
            f"(conhecidos: {sorted(_SQL_TIERS.values())})."
        )
    compute = str(config.get("compute", "Provisioned")).strip().lower()
    if compute not in _SQL_COMPUTE:
        raise ConfigTranslationError(
            f"compute {config.get('compute')!r} não é um computeTier da "
            f"calculadora (conhecidos: {sorted(_SQL_COMPUTE.values())})."
        )
    hardware = str(config.get("hardware", "Gen5")).strip().lower()
    generation = _SQL_GENERATIONS.get(hardware)
    if generation is None:
        raise ConfigTranslationError(
            f"hardware {config.get('hardware')!r} não tem geração equivalente "
            f"na UI (conhecidos: {sorted(set(_SQL_GENERATIONS.values()))})."
        )
    vcores = config.get("vCores")
    if vcores is None:
        raise ConfigTranslationError(
            "config['vCores'] é obrigatório para 'sql' na calculadora: o "
            "<select instanceSize> não tem opção 'não especificado'."
        )
    if int(vcores) not in _SQL_VCORES:
        raise ConfigTranslationError(
            f"vCores={vcores} não é uma opção do instanceSize da calculadora "
            f"(disponíveis: {list(_SQL_VCORES)})."
        )
    _require_consumption(config, "SQL", "reservas e savings plans")

    # Ordem = ordem do painel. purchaseModel/vcoreTier vêm ANTES de
    # generation/instanceSize porque trocá-los remonta as opções seguintes.
    ui["region"] = _region_to_ui_label(config["region"])
    ui["purchaseModel"] = "vCore"  # a config fala em vCores, então não é DTU
    # SEMPRE emitido: o default da UI é Hyperscale, o do resolver é General Purpose.
    ui["vcoreTier"] = _SQL_TIERS[tier]
    ui["computeTier"] = _SQL_COMPUTE[compute]
    ui["generation"] = generation
    ui["instanceSize"] = f"{int(vcores)} vCore"
    # resolve_sql descarta explicitamente os meters "Zone Redundancy", então o
    # lado da UI tem que ficar coerente com isso.
    ui["zoneRedundancy"] = "Locally Redundant"
    # SEMPRE emitidos — são os defaults mais perigosos do painel inteiro
    # (3 anos reservados + BYOL, ~53% abaixo do on-demand). O resolver pediu
    # priceType Consumption e o meter dele é on-demand, então os dois radios
    # têm que dizer a mesma coisa.
    ui["databaseBillingOption"] = "payg"
    # licenseIncluded espelha exatamente este radio: é a MESMA decisão dos dois
    # lados do projeto. Desde 21/08 a trilha de preço cobra (ou não) a licença
    # conforme esse campo (pricing.sql_monthly_cost), então deixá-lo fixo aqui
    # faria as duas metades discordarem: a estimativa sem licença e o link com
    # licença, ou vice-versa.
    license_included = bool(config.get("licenseIncluded", True))
    ui["softwareBillingOption"] = (
        "license_included" if license_included else "azure_hybrid_benefit"
    )

    assumptions.append(
        "SQL: 'type' ficou no default da calculadora (=Single Database); o "
        "produto da Retail API é 'Single/Elastic Pool', que não distingue os dois."
    )
    assumptions.append(
        "SQL: o item da calculadora soma storage de dados e backup à linha de "
        "compute; o nosso número cobre compute + licença. Em GP/Gen5/2 vCore "
        "isso deixou o total da calculadora ~1,3% acima (storage 32 GB de "
        "default + backup), não mais os ~66% da licença."
        if license_included
        else "SQL: Azure Hybrid Benefit (BYOL) — a licença não é cobrada em "
        "nenhuma das duas metades. Storage de dados e backup seguem por conta "
        "da calculadora."
    )
    if quantity != 1:
        unsupported.append(
            f"quantity={quantity}: o painel de SQL não tem campo de quantidade "
            "mapeado; adicione o item mais de uma vez se precisar."
        )
    for extra in sorted(set(usage)):
        unsupported.append(
            f"usage[{extra!r}]: o painel de SQL cobra por hora cheia do mês; "
            "não há campo de horas mapeado."
        )
    return Translation(ui, assumptions, unsupported)


_TRANSLATORS = {
    "vm": _translate_vm,
    "storage": _translate_storage,
    "sql": _translate_sql,
}


def translate_config(
    service: str,
    config: dict,
    usage: dict | None = None,
    quantity: int = 1,
) -> Translation:
    """Traduz a config de um resolver para a config da UI da calculadora.

    `service`/`config` são os MESMOS que resolve_price recebe (e os mesmos que
    os padrões da Skill trazem), então quem já montou uma estimativa de preço
    não precisa remontar nada para gerar o link.

    Levanta ConfigTranslationError para serviço sem tradutor ou valor sem
    rótulo conhecido na UI — nunca manda um palpite para a calculadora.
    """
    translator = _TRANSLATORS.get(service)
    if translator is None:
        raise ConfigTranslationError(
            f"Serviço {service!r} não tem tradução para a UI da calculadora. "
            f"Conhecidos: {sorted(_TRANSLATORS)}"
        )
    if "region" not in config:
        raise ConfigTranslationError(
            f"config['region'] é obrigatório para traduzir {service!r}."
        )
    return translator(config, usage or {}, quantity)
