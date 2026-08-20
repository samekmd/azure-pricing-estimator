import asyncio
from azure_estimator_mcp.azure.catalog import search_services, config_schema
from azure_estimator_mcp.azure.pricing import resolve_price, monthly_cost


async def main():
    matches = await search_services("banco de dados sob demanda")
    print("Matches:", matches)
    key = matches[0].key
    schema = await config_schema(key, region="eastus")
    print("Example config:", schema.example_config)
    print("Notes:", schema.notes)

    config = dict(schema.example_config)
    price = await resolve_price(key, config)
    print("Preço:", price.unit_price, price.currency, "/", price.unit_of_measure)


asyncio.run(main())