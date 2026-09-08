"""Servidor MCP do estimador de preços do Azure.

As tools de descoberta (search_azure_services, get_service_config_schema) e de
preço (resolve_price, estimate_monthly_cost) estão ligadas a azure/catalog.py e
azure/pricing.py; as 6 de calculadora (check_calculator_auth, create_estimate,
add_line_item, edit_line_item, export_estimate, close_calculator) estão ligadas
a azure/calculator_client.py.
Aqui elas são só adaptadores: traduzem argumentos, chamam a função e serializam.

SESSÃO DE NAVEGADOR PERSISTENTE — as tools de calculadora não são independentes
entre si: add_line_item e export_estimate operam sobre a PÁGINA que
create_estimate abriu (guardada em AzureCalculatorClient._page). Um
`async with AzureCalculatorClient()` por tool abriria e fecharia um Chromium a
cada chamada e perderia a estimativa entre elas. Por isso o cliente é um
singleton de módulo, inicializado sob demanda e reaproveitado — ver
_get_calculator().

O navegador NÃO vive para sempre: o `lifespan` do servidor o fecha no
encerramento, a tool close_calculator o fecha sob demanda, e um watchdog o fecha
por ociosidade — mas só quando não há estimativa aberta, porque fechar com uma
estimativa em construção destruiria o trabalho do usuário sem aviso.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .azure.calculator_client import AzureCalculatorClient, CalculatorAuthError
from .azure.catalog import DEFAULT_REGION, DEFAULT_SAMPLE
from .azure.catalog import config_schema as _config_schema
from .azure.catalog import search_services as _search_services
from .azure.pricing import monthly_cost as _monthly_cost
from .azure.pricing import sql_monthly_cost as _sql_monthly_cost
from .azure.config_translate import translate_config as _translate_config
from .azure.pricing import resolve_price as _resolve_price

# server.py -> azure_estimator_mcp -> src -> mcp-server -> raiz do repo.
# O caminho do estado de sessão é resolvido a partir do arquivo, não do cwd:
# um servidor MCP é iniciado pelo cliente, de um diretório qualquer.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STORAGE_STATE = _REPO_ROOT / ".auth" / "storage_state.json"
# Headless por padrão; AZURE_CALC_HEADED=1 abre o navegador visível (depuração).
_HEADLESS = os.environ.get("AZURE_CALC_HEADED", "") not in ("1", "true", "True")

# CICLO DE VIDA DO NAVEGADOR — o singleton não pode viver para sempre (era um
# Chromium aberto até o processo morrer, sem ninguém para fechá-lo), mas também
# não pode ser fechado a esmo: fechar com uma estimativa aberta DESTRÓI o
# trabalho do usuário em silêncio, e a estimativa só existe na página (não há
# rascunho salvo). Por isso o fechamento por ociosidade só acontece quando NÃO
# há estimativa aberta — ou seja, antes da primeira ou depois de um export
# bem-sucedido, quando o link já preserva o resultado.
_IDLE_TIMEOUT_SECONDS = float(os.environ.get("AZURE_CALC_IDLE_TIMEOUT", "900"))
_IDLE_CHECK_INTERVAL = 60.0

_calculator: AzureCalculatorClient | None = None
_estimate_open = False
_last_used = 0.0
# Serializa o acesso ao navegador: as tools podem ser chamadas em paralelo pelo
# cliente MCP, mas há UMA página compartilhada — dois add_line_item concorrentes
# embaralhariam os campos de itens diferentes.
_calculator_lock = asyncio.Lock()


def _touch() -> None:
    """Marca a última vez que uma tool usou o navegador (relógio monotônico)."""
    global _last_used
    _last_used = time.monotonic()


async def _close_calculator_unlocked() -> bool:
    """Fecha o navegador e zera o singleton. Quem chama já tem o lock."""
    global _calculator, _estimate_open
    if _calculator is None:
        return False
    client, _calculator = _calculator, None
    _estimate_open = False
    await client.__aexit__(None, None, None)  # noqa: PLC2801 — par do __aenter__
    return True


def _idle_expired(now: float) -> bool:
    """Decide se o navegador ocioso pode ser fechado AGORA.

    Separado do laço do watchdog para ser testável sem esperar relógio. As três
    condições: existe navegador, não há estimativa aberta (fechar destruiria o
    trabalho) e passou o tempo de ociosidade.
    """
    if _calculator is None or _estimate_open:
        return False
    return (now - _last_used) >= _IDLE_TIMEOUT_SECONDS


async def _idle_watchdog() -> None:
    """Fecha o navegador ocioso. Cancelado no encerramento do servidor."""
    while True:
        await asyncio.sleep(_IDLE_CHECK_INTERVAL)
        async with _calculator_lock:
            if _idle_expired(time.monotonic()):
                await _close_calculator_unlocked()


@contextlib.asynccontextmanager
async def _lifespan(server: MCPServer) -> AsyncIterator[None]:
    """Sobe o watchdog e garante que o Chromium seja fechado no shutdown.

    Sem isto o navegador só morria junto com o processo — e ficava órfão quando
    o cliente MCP matava o servidor.
    """
    watchdog = asyncio.create_task(_idle_watchdog())
    try:
        yield
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
        # Best-effort: no shutdown não há tool em voo para disputar o lock, e
        # falhar aqui não pode impedir o servidor de encerrar.
        with contextlib.suppress(Exception):
            await _close_calculator_unlocked()


mcp = MCPServer("azure-pricing-estimator", lifespan=_lifespan)


async def _get_calculator() -> AzureCalculatorClient:
    """Devolve o cliente de calculadora, iniciando-o na primeira chamada.

    Usa __aenter__ manualmente (em vez de `async with`) de propósito: a sessão
    tem que sobreviver ao fim desta função e ser reaproveitada pelas tools
    seguintes — ver a nota sobre sessão persistente no topo do módulo.
    """
    global _calculator
    if _calculator is None:
        client = AzureCalculatorClient(
            storage_state_path=str(_STORAGE_STATE), headless=_HEADLESS
        )
        await client.__aenter__()  # noqa: PLC2801 — sessão de vida longa
        _calculator = client
    _touch()
    return _calculator


async def _require_authenticated(client: AzureCalculatorClient) -> None:
    """Falha cedo e com mensagem clara quando a sessão não está válida.

    Vale especialmente para export_estimate: o item "Share" da calculadora
    continua HABILITADO quando deslogado (o "Log in to Share" é rótulo, não
    `disabled`), então sem esta checagem a falha viria como um timeout opaco de
    10s do Playwright em vez de "refaça o bootstrap".
    """
    if not await client.is_authenticated():
        raise CalculatorAuthError(
            "Sessão da calculadora expirada ou inválida. Rode novamente: "
            "uv run python mcp-server/scripts/bootstrap_login.py"
        )


@mcp.tool()
async def search_azure_services(
    query: str, currency: str = "USD"
) -> list[dict[str, Any]]:
    """Encontra serviços Azure a partir de um termo em linguagem natural.

    Devolve, para cada candidato, o `service_name` EXATO da Retail Prices API, a
    `key` a ser usada em resolve_price/get_service_config_schema, um rótulo e a
    família do serviço. Termo sem correspondência devolve lista vazia.
    """
    matches = await _search_services(query, currency=currency)
    return [m.model_dump() for m in matches]


@mcp.tool()
async def get_service_config_schema(
    service: str,
    region: str = DEFAULT_REGION,
    currency: str = "USD",
    sample_size: int = DEFAULT_SAMPLE,
) -> dict[str, Any]:
    """Campos de config que um serviço espera, com os valores válidos da API.

    `service` aceita a key ('vm'), o serviceName ('Virtual Machines') ou um
    alias. Enumerações grandes vêm recortadas: amostra + total + a consulta da
    API que produz a lista completa. O `example_config` devolvido já é uma config
    aceita por resolve_price.
    """
    schema = await _config_schema(
        service, region=region, currency=currency, sample_size=sample_size
    )
    return schema.model_dump()


@mcp.tool()
async def resolve_price(
    service: str, config: dict[str, Any], currency: str = "USD"
) -> dict[str, Any]:
    """Resolve o preço unitário de um serviço Azure a partir da sua config."""
    price = await _resolve_price(service, config, currency=currency)
    return price.model_dump()


@mcp.tool()
async def estimate_monthly_cost(
    service: str,
    config: dict[str, Any],
    usage: dict[str, Any],
    currency: str = "USD",
) -> float:
    """Estima o custo mensal de um serviço a partir de config + uso.

    Para `sql` o número devolvido é compute + LICENÇA, não só o compute: a
    licença do SQL Server é um meter separado (sem região, cobrado por
    vCore/hora) e responde por ~66% a mais que o compute sozinho. Por isso
    `vCores` é obrigatório na config de `sql`. Para Azure Hybrid Benefit
    (BYOL), passe `licenseIncluded: false` e a licença não é somada.

    Consequência: para `sql`, este total NÃO é `resolve_price` x horas —
    `resolve_price('sql', ...)` devolve só a linha de compute. Para itemizar,
    resolva também `sql_license` (preço por vCore/hora).
    """
    if service == "sql":
        breakdown = await _sql_monthly_cost(config, usage, currency=currency)
        return breakdown["total"]
    price = await _resolve_price(service, config, currency=currency)
    return _monthly_cost(price, usage)


@mcp.tool()
async def check_calculator_auth() -> bool:
    """Reporta se a sessão salva ainda está logada na calculadora Azure.

    `False` significa sessão expirada (o cookie `jwt` da calculadora é o que
    vence primeiro) — a correção é rodar de novo
    `uv run python mcp-server/scripts/bootstrap_login.py`. Não há renovação
    automática por design: a autenticação é delegada a um navegador real.
    """
    async with _calculator_lock:
        client = await _get_calculator()
        return await client.is_authenticated()


@mcp.tool()
async def create_estimate() -> dict[str, Any]:
    """Abre uma estimativa nova e vazia na calculadora.

    Precisa vir ANTES de add_line_item/export_estimate — as duas operam sobre a
    página aberta aqui. Chamar de novo começa uma estimativa do zero.
    """
    global _estimate_open
    async with _calculator_lock:
        client = await _get_calculator()
        await _require_authenticated(client)
        await client.create_estimate()
        # A partir daqui existe trabalho que só vive na página: o watchdog de
        # ociosidade não pode fechar o navegador até um export bem-sucedido.
        _estimate_open = True
    return {"status": "ok", "message": "Estimativa vazia aberta na calculadora."}


@mcp.tool()
async def add_line_item(
    service: str,
    config: dict[str, Any],
    usage: dict[str, Any] | None = None,
    quantity: int = 1,
) -> dict[str, Any]:
    """Adiciona um serviço à estimativa aberta, a partir da config do resolver.

    `service`/`config` são os MESMOS que resolve_price recebe (e os mesmos que
    os padrões da Skill trazem): a tradução para o vocabulário da UI
    (`armSkuName` -> `size`, `windows` -> `operatingSystem`, `Gen5` ->
    `Standard-series (Gen 5)`...) é feita aqui por azure/config_translate.py.
    Não monte a config no vocabulário da calculadora.

    `usage` e `quantity` vêm do componente do padrão: `usage['hours']` vira o
    campo de horas da VM e `quantity` vira o campo de instâncias.

    A resposta traz `item_id` — o ENDEREÇO deste item na estimativa, para
    edit_line_item. Guarde-o: com vários componentes do mesmo serviço
    (web-tier e app-tier são ambos `vm`), é a única forma de dizer depois qual
    item se quer corrigir.

    A resposta traz também `applied` (o que foi de fato aplicado na UI), `assumptions`
    (campos da UI que a config não determina e ficaram no default) e
    `unsupported` (o que NÃO pôde ser aplicado — ex.: `quantity` > 1 para
    storage, porque o painel não tem campo de quantidade de contas: o `count`
    dali é capacidade). Mostre as duas últimas listas ao usuário: são a
    diferença entre o que ele pediu e o que a calculadora recebeu.
    """
    traducao = _translate_config(service, config, usage or {}, quantity)
    async with _calculator_lock:
        client = await _get_calculator()
        item_id = await client.add_line_item(service, traducao.ui_config)
    return {
        "status": "ok",
        "item_id": item_id,
        "service": service,
        "applied": traducao.ui_config,
        "assumptions": traducao.assumptions,
        "unsupported": traducao.unsupported,
    }


@mcp.tool()
async def export_estimate() -> str:
    """Compartilha a estimativa aberta e devolve o link público da calculadora.

    O link (formato `https://azure.com/e/<id>`) abre a estimativa completa para
    quem não tem sessão. Exige estar autenticado: a checagem é feita antes de
    clicar em Share, porque o menu da calculadora não desabilita a opção quando
    deslogado — falharia com um timeout opaco.
    """
    global _estimate_open
    async with _calculator_lock:
        client = await _get_calculator()
        await _require_authenticated(client)
        link = await client.export_estimate()
        # O link preserva a estimativa; a partir daqui o navegador pode ser
        # fechado por ociosidade sem destruir trabalho.
        _estimate_open = False
        return link


@mcp.tool()
async def edit_line_item(
    item_id: str,
    config: dict[str, Any],
    usage: dict[str, Any] | None = None,
    quantity: int = 1,
) -> dict[str, Any]:
    """Corrige um item JÁ adicionado, endereçado pelo `item_id`.

    `item_id` é o que add_line_item devolveu. O serviço é deduzido do próprio
    id, então não é preciso (nem possível) informá-lo errado.

    `config`/`usage`/`quantity` são os MESMOS de add_line_item — a config
    COMPLETA do componente, no vocabulário do resolver, não só o campo que
    mudou: a tradução reaplica todos os campos que a config determina, o que
    torna a edição idempotente e evita deixar o item num estado misto.
    """
    service = AzureCalculatorClient.service_of_item(item_id)
    traducao = _translate_config(service, config, usage or {}, quantity)
    async with _calculator_lock:
        client = await _get_calculator()
        await client.edit_line_item(item_id, traducao.ui_config)
    return {
        "status": "ok",
        "item_id": item_id,
        "service": service,
        "applied": traducao.ui_config,
        "assumptions": traducao.assumptions,
        "unsupported": traducao.unsupported,
    }


@mcp.tool()
async def close_calculator() -> dict[str, Any]:
    """Fecha o navegador da calculadora e libera os recursos.

    DESCARTA a estimativa aberta — ela só existe na página; exporte antes se
    quiser preservá-la. A próxima chamada de create_estimate reabre o navegador
    de forma transparente, mas os `item_id` de antes deixam de valer.
    """
    async with _calculator_lock:
        fechou = await _close_calculator_unlocked()
    return {
        "status": "ok",
        "closed": fechou,
        "message": (
            "Navegador fechado." if fechou else "Nenhum navegador estava aberto."
        ),
    }


if __name__ == "__main__":
    mcp.run()
