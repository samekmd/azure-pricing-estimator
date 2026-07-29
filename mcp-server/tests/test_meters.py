"""Testes dos resolvers config->meter (mockados, sem rede)."""

import pytest

from azure_estimator_mcp.azure.meters import (
    PriceResolutionError,
    resolve_sql,
    resolve_storage,
    resolve_vm,
)


# --------------------------------- VM -------------------------------------- #
def test_resolve_vm_isolates_linux_excludes_spot_and_windows(item_factory):
    linux = item_factory(meterName="D2s v3", skuName="D2s v3",
                         productName="Virtual Machines Dv3 Series")
    spot = item_factory(meterName="D2s v3 Spot", skuName="D2s v3 Spot")
    lowpri = item_factory(meterName="D2s v3 Low Priority", skuName="D2s v3 Low Priority")
    windows = item_factory(productName="Virtual Machines Dv3 Series Windows")

    filters, select = resolve_vm({"armSkuName": "Standard_D2s_v3", "region": "eastus"})
    assert filters["serviceName"] == "Virtual Machines"
    assert filters["armRegionName"] == "eastus"

    chosen = select([linux, spot, lowpri, windows])
    assert chosen is linux


def test_resolve_vm_windows_variant(item_factory):
    linux = item_factory(productName="Virtual Machines Dv3 Series")
    windows = item_factory(productName="Virtual Machines Dv3 Series Windows")
    _, select = resolve_vm(
        {"armSkuName": "Standard_D2s_v3", "region": "eastus", "windows": True}
    )
    assert select([linux, windows]) is windows


def test_resolve_vm_zero_candidates_raises(item_factory):
    only_spot = item_factory(meterName="D2s v3 Spot")
    _, select = resolve_vm({"armSkuName": "Standard_D2s_v3", "region": "eastus"})
    with pytest.raises(PriceResolutionError):
        select([only_spot])


def test_resolve_vm_ambiguous_raises_with_candidates(item_factory):
    a = item_factory(meterName="D2s v3", skuName="D2s v3")
    b = item_factory(meterName="D2s v3", skuName="D2s v3", meterId="dup")
    _, select = resolve_vm({"armSkuName": "Standard_D2s_v3", "region": "eastus"})
    with pytest.raises(PriceResolutionError) as exc:
        select([a, b])
    assert len(exc.value.candidates) == 2


# ------------------------------- Storage ----------------------------------- #
def test_resolve_storage_blob_capacity(item_factory):
    capacity = item_factory(
        serviceName="Storage", productName="Blob Storage",
        skuName="Hot LRS", meterName="Hot LRS Data Stored",
        unitOfMeasure="1 GB/Month",
    )
    transactions = item_factory(
        serviceName="Storage", productName="Blob Storage",
        skuName="Hot LRS", meterName="Hot LRS Write Operations",
    )
    other_redundancy = item_factory(
        serviceName="Storage", productName="Blob Storage",
        skuName="Hot GRS", meterName="Hot GRS Data Stored",
    )
    _, select = resolve_storage(
        {"region": "eastus", "redundancy": "LRS", "tier": "Hot"}
    )
    chosen = select([capacity, transactions, other_redundancy])
    assert chosen is capacity


def test_resolve_storage_ambiguous_raises(item_factory):
    a = item_factory(productName="Blob Storage", skuName="Hot LRS",
                     meterName="Hot LRS Data Stored")
    b = item_factory(productName="Blob Storage", skuName="Hot LRS",
                     meterName="Hot LRS Data Stored", meterId="dup")
    _, select = resolve_storage({"region": "eastus", "redundancy": "LRS", "tier": "Hot"})
    with pytest.raises(PriceResolutionError):
        select([a, b])


# --------------------------------- SQL ------------------------------------- #
def test_resolve_sql_provisioned_isolates(item_factory):
    provisioned = item_factory(
        serviceName="SQL Database",
        productName="SQL Database Single General Purpose - Compute Gen5",
        skuName="2 vCore", meterName="2 vCore",
    )
    serverless = item_factory(
        serviceName="SQL Database",
        productName="SQL Database Single General Purpose - Serverless - Compute Gen5",
        skuName="2 vCore Serverless", meterName="2 vCore Serverless",
    )
    other_vcore = item_factory(
        serviceName="SQL Database",
        productName="SQL Database Single General Purpose - Compute Gen5",
        skuName="4 vCore", meterName="4 vCore",
    )
    _, select = resolve_sql(
        {"region": "eastus", "tier": "General Purpose",
         "compute": "Provisioned", "vCores": 2}
    )
    chosen = select([provisioned, serverless, other_vcore])
    assert chosen is provisioned


def test_resolve_sql_serverless(item_factory):
    serverless = item_factory(
        productName="SQL Database Single General Purpose - Serverless - Compute Gen5",
        skuName="2 vCore", meterName="2 vCore",
    )
    provisioned = item_factory(
        productName="SQL Database Single General Purpose - Compute Gen5",
        skuName="2 vCore", meterName="vCore",
    )
    _, select = resolve_sql(
        {"region": "eastus", "tier": "General Purpose",
         "compute": "Serverless", "vCores": 2}
    )
    assert select([serverless, provisioned]) is serverless


def test_resolve_sql_identical_duplicates_collapse(item_factory):
    """Linhas idênticas do mesmo meter (mesmo meterId) não são ambíguas."""
    dup_a = item_factory(
        productName="SQL Database Single/Elastic Pool General Purpose - Compute Gen5",
        skuName="2 vCore", meterName="vCore", meterId="same-id", retailPrice=0.30,
    )
    dup_b = item_factory(
        productName="SQL Database Single/Elastic Pool General Purpose - Compute Gen5",
        skuName="2 vCore", meterName="vCore", meterId="same-id", retailPrice=0.30,
    )
    _, select = resolve_sql(
        {"region": "eastus", "tier": "General Purpose",
         "compute": "Provisioned", "vCores": 2}
    )
    chosen = select([dup_a, dup_b])
    assert chosen["meterId"] == "same-id"


def test_resolve_sql_zero_candidates_raises(item_factory):
    wrong_tier = item_factory(productName="SQL Database Business Critical", skuName="2 vCore")
    _, select = resolve_sql(
        {"region": "eastus", "tier": "General Purpose", "vCores": 2}
    )
    with pytest.raises(PriceResolutionError):
        select([wrong_tier])
