# Azure Pricing Estimator

Ferramenta que transforma a **descrição de uma arquitetura** em um **link compartilhável da calculadora de preços da Azure**, com o custo já estimado. O arquiteto de soluções descreve a arquitetura em linguagem natural — muitas vezes um padrão conhecido — e o sistema interpreta, resolve os preços e devolve o link.

> Escopo atual: **somente Azure**, a **preço público** (retail). Databricks e preços negociados ficam fora desta versão.

---

## O que é

O projeto se apoia em duas peças complementares, cada uma com um papel:

- **MCP** (as *capacidades*): expõe ferramentas que alcançam sistemas externos — consultar a Azure Retail Prices API, resolver `config → meter`, validar SKU e gerar o link da calculadora.
- **Skill** (o *conhecimento*): carrega a biblioteca de arquiteturas de referência, as regras de interpretação e validação, e a orquestração de quando chamar cada ferramenta.

A "interpretação" da descrição não é um pipeline de NLU que a gente escreve: é o próprio agente (Claude Code) raciocinando, guiado pela Skill e usando as ferramentas do MCP.

## Como funciona (fluxo)

```
Descrição do arquiteto (linguagem natural)
        │
        ▼
Interpretação  →  casa com a biblioteca de padrões (determinístico)
                  ou extrai serviços via LLM (casos não-padrão)
        │
        ▼
Resolução      →  config → meter, valida SKU e resolve preço (Retail Prices API)
        │
        ▼
Saída          →  monta a estimativa e devolve o LINK da calculadora Azure + preview do custo
```

## Decisões técnicas principais

- **Link nativo via navegador (Playwright).** O endpoint interno de save da calculadora (`/api/v2/calculator/shared-estimates/save/`) exige uma sessão de navegador autenticada (cookie + token CSRF). A autenticação é delegada a um Chromium real, no qual o login é feito manualmente uma vez e a sessão é persistida.
- **OAuth foi testado e descartado.** Tentamos autenticar o endpoint com um Bearer token do Entra ID; a Microsoft bloqueia (`AADSTS65002`) — é uma API *first-party* não liberada para consumo externo. Registro da investigação em `test_bearer_auth.py`.
- **Preço público via Retail Prices API.** Fonte pública e sem autenticação (`https://prices.azure.com/api/retail/prices`), com filtros OData e paginação. Como o preço não é negociado, dispensa Price Sheet API e credenciais de billing.

## Estrutura do projeto

```
azure-pricing-estimator/
├── README.md
├── PROGRESS.md                       # acompanhamento detalhado por fase
├── pyproject.toml / uv.lock          # projeto gerenciado com uv
├── .gitignore                        # inclui .auth/ (sessão real, nunca commitar)
├── .mcp.json                         # (Fase 2) config do MCP p/ Claude Code
├── test_bearer_auth.py               # investigação OAuth — descartada
│
├── skill/                            # (Fase 3) o CONHECIMENTO
│   └── azure-arch-estimator/
│       ├── SKILL.md
│       ├── patterns/                 # arquiteturas de referência (YAML)
│       │   ├── three-tier-web-app.yaml
│       │   ├── aks-microservices.yaml
│       │   └── data-lakehouse.yaml
│       └── reference/
│           ├── interpretation-guide.md
│           └── validation-rules.md
│
└── mcp-server/                       # as CAPACIDADES
    ├── scripts/
    │   └── bootstrap_login.py        # (Fase 0) login manual → salva a sessão
    ├── src/azure_estimator_mcp/
    │   ├── server.py                 # (Fase 2) FastMCP: registra as tools
    │   ├── models.py                 # (Fase 1) PriceResult, etc.
    │   ├── tools/                    # (Fase 2) as 6 tools do MCP
    │   │   ├── search.py             #   search_azure_services
    │   │   ├── schema.py             #   get_service_config_schema
    │   │   ├── resolve.py            #   resolve_price
    │   │   ├── estimate.py           #   create_estimate, add_line_item
    │   │   └── export.py             #   export_estimate
    │   └── azure/
    │       ├── retail_client.py      # (Fase 1) Retail Prices API + paginação
    │       ├── meters.py             # (Fase 1) config → meter
    │       └── calculator_client.py  # (Fase 0) driver Playwright
    └── tests/
        ├── test_pagination.py
        ├── test_meters.py
        └── test_resolve_integration.py
```

## Stack

| Camada          | Tecnologia |
|-----------------|------------|
| Linguagem       | Python 3.12+ |
| Gerência de projeto | uv |
| MCP             | SDK oficial (FastMCP) |
| HTTP (preço)    | httpx (async) |
| Modelos         | pydantic v2 |
| Padrões         | PyYAML |
| Navegador       | Playwright (Chromium) |
| Testes          | pytest, respx |

## Setup

```bash
# 1. Dependências
uv sync

# 2. Navegador do Playwright (uma vez)
uv run playwright install chromium

# 3. Login manual — abre um Chromium visível; faça login (com MFA) e pressione Enter.
#    Salva a sessão em .auth/storage_state.json
uv run python mcp-server/scripts/bootstrap_login.py
```

> A sessão expira depois de um tempo. Quando `is_authenticated()` retornar `False`, basta rodar o bootstrap de novo. Não há auto-refresh — é intencional.

## Fases de desenvolvimento

| Fase | Descrição | Status |
|:----:|-----------|:------:|
| 0 | Autenticação — spike do endpoint de save, Playwright, sessão persistida | ✅ concluída |
| 1 | Núcleo de preço — Retail Prices API, `config → meter`, `resolve_price` + testes | 🔄 em andamento |
| 2 | MCP mínimo — as 6 tools + fatia vertical (descrição → link) no Claude Code | ⏳ planejada |
| 3 | Skill — biblioteca de padrões, interpretação e validação | ⏳ planejada |
| 4 | Integração e entrega — teste ponta a ponta, robustez, demo | ⏳ planejada |

**Princípio que atravessa tudo:** resolver o incerto antes do trabalhoso, e fechar uma fatia vertical de ponta a ponta antes de crescer em largura. O núcleo determinístico (Fases 0–1) é código testável que não depende do LLM; o agente e a Skill só entram na Fase 3, orquestrando por cima de ferramentas que já funcionam sozinhas.

---

## Cronograma (29/07 – 28/08)

Três blocos de trabalho. As tarefas de cada fase foram distribuídas para rodarem **em paralelo**: enquanto Samuel fecha o núcleo de preço, André adianta o esqueleto do MCP (com tools *stub*, não depende do preço pronto) e Natália constrói a biblioteca de padrões e a Skill (conhecimento, independente do código).

### Fase 1 — Núcleo de preço · 29/07 – 01/08

| Membro | Tarefa |
|--------|--------|
| **Samuel** | `retail_client.py` + `meters.py` (VM, Storage, SQL) + `resolve_price`, com testes pytest |
| **André** | Esqueleto do servidor MCP: `server.py` (FastMCP) registrando as 6 tools como *stubs*; validar no MCP Inspector |
| **Natália** | Biblioteca de padrões: 3 arquiteturas de referência em YAML (three-tier, AKS, lakehouse) + rascunho do `SKILL.md` |

### Fase 2 — MCP mínimo (fatia vertical) · 04/08 – 08/08

| Membro | Tarefa |
|--------|--------|
| **Samuel** | Ligar `resolve_price` às tools de preço (`search_azure_services`, `get_service_config_schema`, `resolve_price`, `add_line_item`) |
| **André** | Implementar `export_estimate` dirigindo a UI da calculadora via Playwright: mapear seletores (buscar serviço, configurar, compartilhar), capturar o link |
| **Natália** | Testar a fatia vertical no Claude Code (pedido cru → link + custo) e registrar bugs e lacunas de configuração |

### Fase 3 — Skill + interpretação · 10/08 – 21/08

| Membro | Tarefa |
|--------|--------|
| **Natália** | Finalizar `SKILL.md` (workflow completo) + `interpretation-guide.md` + `validation-rules.md` |
| **Samuel** | Afinar os resolvers para garantir que todos os serviços dos 3 padrões resolvem preço corretamente |
| **André** | Robustez do export: retry, fallback e detecção de sessão expirada (orienta a refazer o bootstrap) |

### Fase 4 — Integração e entrega · 24/08 – 28/08

| Membro | Tarefa |
|--------|--------|
| **Todos** | Teste ponta a ponta: arquiteto descreve uma arquitetura padrão → recebe link + custo; correção de bugs |
| **Samuel** | Finalizar `README.md`, `PROGRESS.md` e o `.mcp.json` de instalação |
| **André + Natália** | Preparar a demo da entrega: roteiro + caso de exemplo completo |

### Dependências a vigiar

- A Fase 2 do **Samuel** (ligar preço às tools) depende da Fase 1 fechada — por isso ela é a primeira da semana 2.
- A Fase 2 do **André** (export via Playwright) **não** depende do preço: pode começar já na semana 1, pois a base de autenticação (Fase 0) está pronta.
- A Skill da **Natália** é independente do código e pode avançar desde o primeiro dia; ela só se conecta ao resto na Fase 3.

---

## Cronograma (04/09 – 18/09) — segunda entrega

Duas frentes rodando **em paralelo**: enquanto Natália e Cauã redesenham a
biblioteca de padrões (as 3 arquiteturas mapeadas de primeira mão na entrega
anterior), Samuel, Arthur e André trazem o
[AzurePricingMCP](https://github.com/msftnadavbh/AzurePricingMCP) — um MCP de
comunidade para consulta de preços da Azure — para o repositório, adequam-no
aos padrões de entrega do projeto e tentam integrá-lo ao MCP próprio
(construído com Playwright).

### Bloco 1 — Descoberta e planejamento · 07/09 – 11/09

| Membro | Tarefa |
|--------|--------|
| **Natália** | Revisar os 3 padrões atuais (`three-tier-web-app`, `aks-microservices`, `data-lakehouse`) e levantar o que muda no redesenho (componentes desatualizados, lacunas) |
| **Cauã** | Levantar referências de arquitetura para orientar as novas versões dos 3 padrões |
| **Samuel** | Rodar o AzurePricingMCP localmente e mapear suas tools/capacidades |
| **Arthur** | Comparar as tools do AzurePricingMCP com as do nosso MCP (`resolve_price`, `search_azure_services` etc.) e listar sobreposições/lacunas |
| **André** | Levantar os padrões de entrega do projeto (estrutura de `mcp-server/`, testes, convenções) que o AzurePricingMCP precisa seguir para ser incorporado |

### Bloco 2 — Execução · 14/09 – 16/09

| Membro | Tarefa |
|--------|--------|
| **Natália** | Redesenhar os 3 padrões (novas versões dos YAMLs de arquitetura) |
| **Cauã** | Validar os padrões redesenhados contra os resolvers existentes |
| **Samuel** | Adequar o código do AzurePricingMCP à estrutura/testes do projeto |
| **Arthur + André** | Primeira tentativa de integração entre o AzurePricingMCP e o MCP Playwright; comparar preços retornados por ambos para checar consistência |

### Bloco 3 — Fechamento e entrega · 17/09 – 18/09

| Membro | Tarefa |
|--------|--------|
| **Natália + Cauã** | Testar as arquiteturas redesenhadas ponta a ponta e atualizar a documentação da Skill/padrões |
| **Samuel + Arthur + André** | Fechar a integração do AzurePricingMCP, corrigir bugs e preparar a demo |
| **Todos** | Teste ponta a ponta da entrega, revisão final |

### Dependências a vigiar

- O redesenho de **Natália/Cauã** é independente da integração do MCP de
  comunidade — as duas frentes correm em paralelo desde o dia 1.
- A tentativa de integração (**Arthur + André**, Bloco 2) depende do
  levantamento de tools do Bloco 1 (Arthur) e da adequação de padrões feita
  por Samuel — por isso só começa na semana 2.

---

## Equipe

- **Samuel** — núcleo de preço, integração das tools, documentação
- **André** — servidor MCP, automação do navegador (export)
- **Natália** — Skill, biblioteca de padrões, testes de ponta a ponta
- **Arthur** — integração do MCP de comunidade (AzurePricingMCP) com o MCP próprio
- **Cauã** — redesenho da biblioteca de padrões de arquitetura

## Segurança

- `.auth/storage_state.json` contém **credenciais de sessão reais** → está no `.gitignore`, nunca é commitado nem impresso em logs.
- Sem segredos hardcoded no código.
- Tokens de sessão (JWT) capturados durante depuração **não** devem ser colados em issues, commits ou canais públicos — expiram sozinhos em poucas horas.