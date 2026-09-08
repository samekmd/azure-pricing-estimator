"""Teste manual dos 3 patterns (Fase 2) contra a Retail Prices API real.

Itera os componentes dos 3 patterns tal como estão nos YAMLs hoje, chama
resolve_price + estimate_monthly_cost pra cada um, e reporta o que
funcionou, o que bloqueou como esperado, e o que quebrou de forma
inesperada. Não depende de pyyaml -- os componentes estão hardcoded aqui
exatamente como em patterns/*.yaml, pra evitar adicionar dependência nova
só pra teste manual.
"""

import asyncio

from azure_estimator_mcp.azure.meters import PriceResolutionError
from azure_estimator_mcp.azure.pricing import monthly_cost, resolve_price

# servicos que que devem bloquear hoje (sem resolver em meters.py)
KNOWN_BLOCKED = {"aks", "synapse"}

PATTERNS = {
    "three-tier-web-app": [
        ("web-tier", "vm", {"armSkuName": "Standard_D2s_v3", "region": "East US",
                             "windows": False, "priceType": "Consumption"},
         {"hours": 730}, 2),
        ("app-tier", "vm", {"armSkuName": "Standard_D2s_v3", "region": "East US",
                             "windows": False, "priceType": "Consumption"},
         {"hours": 730}, 2),
        ("data-tier", "sql", {"region": "East US", "tier": "General Purpose",
                               "compute": "Provisioned", "hardware": "Gen5",
                               "vCores": 2, "priceType": "Consumption"},
         {}, 1),
        ("static-assets", "storage", {"region": "East US", "redundancy": "LRS",
                                       "tier": "Hot", "priceType": "Consumption"},
         {"gb": 500}, 1),
    ],
    "aks-microservices": [
        ("node-pool", "vm", {"armSkuName": "Standard_D4s_v3", "region": "East US",
                              "windows": False, "priceType": "Consumption"},
         {"hours": 730}, 3),
        ("control-plane-sla", "aks", {"region": "East US", "tier": "Standard"},
         {}, 1),  # esperado: bloqueado
        ("database", "sql", {"region": "East US", "tier": "General Purpose",
                              "compute": "Provisioned", "hardware": "Gen5",
                              "vCores": 2, "priceType": "Consumption"},
         {}, 1),
        ("shared-storage", "storage", {"region": "East US", "redundancy": "LRS",
                                        "tier": "Hot", "priceType": "Consumption"},
         {"gb": 200}, 1),
    ],
    "data-lakehouse": [
        ("data-lake-storage", "storage", {"region": "East US", "redundancy": "LRS",
                                           "tier": "Hot", "priceType": "Consumption"},
         {"gb": 1000}, 1),
        ("synapse-serverless-sql", "synapse", {"region": "East US",
                                                "tier": "Serverless SQL Pool"},
         {"tbProcessed": 5}, 1),  # esperado: bloqueado
    ],
}


async def test_component(component_id, service, config, usage, quantity):
    blocked_expected = service in KNOWN_BLOCKED
    try:
        price = await resolve_price(service, config)
        cost = monthly_cost(price, usage)
        total = cost * quantity
        if blocked_expected:
            print(f"  [INESPERADO] {component_id} ({service}): deveria estar "
                  f"bloqueado mas resolveu! unit_price={price.unit_price} "
                  f"{price.currency}/{price.unit_of_measure}")
            return "unexpected_success"
        print(f"  [OK] {component_id} ({service}): "
              f"${cost:.2f}/mês x{quantity} = ${total:.2f}/mês "
              f"(meter: {price.meter_name})")
        return "ok"
    except PriceResolutionError as exc:
        if blocked_expected:
            print(f"  [OK, bloqueado como esperado] {component_id} ({service}): {exc}")
            return "expected_block"
        print(f"  [BUG?] {component_id} ({service}): PriceResolutionError "
              f"não esperado: {exc}")
        return "unexpected_error"
    except ValueError as exc:
        print(f"  [BUG?] {component_id} ({service}): ValueError em "
              f"monthly_cost: {exc}")
        return "unexpected_error"
    except Exception as exc:
        print(f"  [BUG] {component_id} ({service}): erro não tratado "
              f"({type(exc).__name__}): {exc}")
        return "unhandled_error"


async def main():
    summary = {}
    for pattern_id, components in PATTERNS.items():
        print(f"\n=== {pattern_id} ===")
        pattern_total = 0.0
        results = []
        for component_id, service, config, usage, quantity in components:
            result = await test_component(component_id, service, config, usage, quantity)
            results.append(result)
        summary[pattern_id] = results

    print("\n=== RESUMO ===")
    for pattern_id, results in summary.items():
        counts = {r: results.count(r) for r in set(results)}
        print(f"{pattern_id}: {counts}")
    print(
        "\nRevise linhas [BUG?]/[BUG]/[INESPERADO] a mão -- são candidatas a "
        "gap/bug para registrar. [OK] e [OK, bloqueado como esperado] estão "
        "dentro do previsto pelo SKILL.md."
    )


if __name__ == "__main__":
    asyncio.run(main())