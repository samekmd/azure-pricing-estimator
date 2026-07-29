"""Fixtures e config compartilhadas dos testes da Fase 1.

Deixa o pacote `azure_estimator_mcp` importável (o código vive em src/) e provê
um helper para pular os testes de integração quando não há rede.
"""

import socket
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _has_network(host: str = "prices.azure.com", port: int = 443) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def has_network() -> bool:
    return _has_network()


@pytest.fixture
def item_factory():
    """Cria um Item da Retail Prices API com defaults sensatos + overrides."""

    def make(**overrides):
        base = {
            "retailPrice": 0.1,
            "unitPrice": 0.1,
            "currencyCode": "USD",
            "unitOfMeasure": "1 Hour",
            "serviceName": "Virtual Machines",
            "productName": "Virtual Machines Dv3 Series",
            "skuName": "D2s v3",
            "armSkuName": "Standard_D2s_v3",
            "meterId": "0001-0001",
            "meterName": "D2s v3",
            "armRegionName": "eastus",
            "type": "Consumption",
            "isPrimaryMeterRegion": True,
            "tierMinimumUnits": 0.0,
            "savingsPlan": [],
        }
        base.update(overrides)
        return base

    return make
