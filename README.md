# Azure Pricing Estimator

Ferramenta que transforma a **descrição de uma arquitetura** em um **link compartilhável da calculadora de preços da Azure**, com o custo já estimado. O arquiteto de soluções descreve a arquitetura em linguagem natural — muitas vezes um padrão conhecido — e o sistema interpreta, resolve os preços e devolve o link.

> Escopo atual: **somente Azure**, a **preço público** (retail). Databricks e preços negociados ficam fora desta versão.

---

## O que é

O projeto se apoia em duas peças complementares, cada uma com um papel:

- **MCP** (as _capacidades_): expõe ferramentas que alcançam sistemas externos — consultar a Azure Retail Prices API, resolver `config → meter`, validar SKU e gerar o link da calculadora.
- **Skill** (o _conhecimento_): carrega a biblioteca de arquiteturas de referência, as regras de interpretação e validação, e a orquestração de quando chamar cada ferramenta.

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
- **OAuth foi testado e descartado.** Tentamos autenticar o endpoint com um Bearer token do Entra ID; a Microsoft bloqueia (`AADSTS65002`) — é uma API _first-party_ não liberada para consumo externo. O script da investigação (`test_bearer_auth.py`) foi removido do working tree; o registro fica no histórico do git (commits `e0c24a2` e `c321227`).
- **Preço público via Retail Prices API.** Fonte pública e sem autenticação (`https://prices.azure.com/api/retail/prices`), com filtros OData e paginação. Como o preço não é negociado, dispensa Price Sheet API e credenciais de billing.

---

## Instalação

Pré-requisitos: **Python 3.14+**, [**uv**](https://docs.astral.sh/uv/) e uma conta Microsoft com acesso à calculadora de preços da Azure.

```bash
# 1. Dependências + navegador do Playwright
#    (wraps `uv sync` + `uv run playwright install chromium`)
make install

# 2. Login manual — abre um Chromium visível; faça login (com MFA) e pressione Enter.
#    Salva a sessão em .auth/storage_state.json
make bootstrap-login
```

Depois disso, **abra o Claude Code na raiz do projeto**. O `.mcp.json` versionado
registra o servidor; na primeira vez o Claude Code pede aprovação para carregá-lo.
A raiz importa: o servidor é lançado como `uv run python -m azure_estimator_mcp.server`,
sem caminho absoluto, e é o diretório do projeto que resolve o `uv run`.

A Skill (`.claude/skills/azure-arch-estimator/`) é descoberta automaticamente
pelo Claude Code no mesmo diretório — nada a instalar.

Para conferir que subiu, peça no Claude Code `/mcp`: o servidor
`azure-pricing-estimator` deve aparecer com **10 tools**.

> A sessão do navegador expira depois de um tempo. Quando `check_calculator_auth`
> devolver `False`, rode `make bootstrap-login` de novo. Não há auto-refresh — é
> intencional (ver [Segurança](#segurança)).

## Uso — exemplo real, ponta a ponta

Pedido do arquiteto, em linguagem natural:

> "Preciso estimar uma aplicação web em três camadas no Azure: frontend, backend
> e banco de dados, em East US."

A Skill casa com o padrão `three-tier-web-app`, resolve o preço de cada
componente e monta a estimativa na calculadora. Resultado (executado em
**24/08**, região East US, preço público em USD):

| Componente | Serviço | Config | Nosso cálculo | Item na calculadora |
| --- | --- | --- | ---: | ---: |
| `web-tier` | VM | 2× D2s v3, Linux, 730 h | $140,16 | $140,16 |
| `app-tier` | VM | 2× D2s v3, Linux, 730 h | $140,16 | $140,16 |
| `data-tier` | SQL Database | GP, Gen5, 2 vCore (compute + licença) | $368,19 | $372,97 |
| `static-assets` | Storage | Blob Hot, LRS, 500 GB | $10,40 | $11,44 |
| **Total mensal** | | | **$658,91** | **$664,74** |

Link gerado: **<https://azure.com/e/8baea4c3172e4436959cf0712a929633>**
(abre sem login, com os 4 itens configurados).

A diferença de **$5,83 (0,88%)** é conhecida e declarada, não um erro de
arredondamento: a calculadora soma ao item de SQL o storage de dados ($3,68 com
o default de 32 GB) e o backup ($1,10), e ao item de storage os contadores de
operações ($1,05 nos defaults dela). São campos que o padrão não determina —
`add_line_item` os devolve em `assumptions` para o usuário ver, em vez de o
núcleo de preço chutar um volume.

## As tools do MCP

Dez tools, em duas trilhas que **não se cruzam**: preço é HTTP puro, calculadora
é navegador (ver [Estrutura](#estrutura-do-projeto)).

**Preço (Retail Prices API, sem navegador)**

| Tool | O que faz |
| --- | --- |
| `search_azure_services` | Encontra serviços a partir de um termo em linguagem natural; devolve o `serviceName` exato da API e a `key` usada pelas demais tools. |
| `get_service_config_schema` | Campos de `config` que um serviço espera, com os valores válidos vindos da API e um `example_config` pronto. |
| `resolve_price` | Resolve o **preço unitário** de um serviço a partir da config (um meter, sem adivinhação). |
| `estimate_monthly_cost` | Projeta o custo mensal a partir de `config` + `usage`. Para `sql`, devolve compute **+ licença**. |

**Calculadora (Playwright, exige sessão)**

| Tool | O que faz |
| --- | --- |
| `check_calculator_auth` | Diz se a sessão salva ainda está logada. `False` = rodar `bootstrap_login.py`. |
| `create_estimate` | Abre uma estimativa nova e vazia. Precede `add_line_item`/`export_estimate`. |
| `add_line_item` | Adiciona um serviço à estimativa **com a config do resolver** (a tradução para o vocabulário da UI é interna). Devolve `item_id`, `applied`, `assumptions` e `unsupported`. |
| `edit_line_item` | Reaplica campos em **um** item específico, endereçado pelo `item_id`. |
| `export_estimate` | Compartilha a estimativa e devolve o link público (`https://azure.com/e/<id>`). |
| `close_calculator` | Fecha o navegador. **Descarta** a estimativa aberta — exporte antes. |

## Serviços suportados e limitações

| service | Resolve preço | Entra no link |
| --- | :---: | :---: |
| `vm` | ✅ | ✅ |
| `storage` | ✅ | ✅ |
| `sql` | ✅ | ✅ |
| `sql_license` | ✅ (linha separada, já somada em `sql`) | — |
| `aks` | ✅ (só a taxa do control plane) | ❌ |
| `synapse` | ✅ (só o serverless SQL pool) | ❌ |

- `aks` e `synapse` **têm preço mas não têm link**: falta o mapeamento em
  `config_translate.py`, e `add_line_item` recusa com erro claro em vez de
  aplicar uma config chutada. Os nós de um cluster AKS não têm esse problema —
  são VMs comuns, cobertas por `vm`.
- **Quantidade de contas de storage** não é aplicável no link: o campo `count`
  do painel de storage é capacidade, não quantidade. Vem em `unsupported`.
- No item de SQL, storage de dados e backup ficam por conta dos defaults da
  calculadora (≈1,3% do item).
- `quantity` é aplicado no link (campo de instâncias), mas `resolve_price` e
  `estimate_monthly_cost` devolvem valor **por unidade** — a multiplicação é da
  Skill.

## Estrutura do projeto

```
azure-pricing-estimator/
├── README.md
├── CLAUDE.md                         # convenções e armadilhas, para quem editar o código
├── pyproject.toml / uv.lock          # projeto gerenciado com uv
├── Makefile                          # install, test, test-integration, test-browser, bootstrap-login
├── .mcp.json                         # registro do servidor MCP (aprovado pelo Claude Code)
├── .gitignore                        # inclui .auth/ (sessão real, nunca commitar)
│
├── .claude/skills/azure-arch-estimator/   # o CONHECIMENTO (Skill)
│   ├── SKILL.md                      # fluxo: interpretar → resolver → montar → exportar
│   ├── patterns/                     # arquiteturas de referência (YAML)
│   │   ├── three-tier-web-app.yaml
│   │   ├── aks-microservices.yaml
│   │   └── data-lakehouse.yaml
│   └── reference/
│       ├── interpretation-guide.md
│       └── validation-rules.md
│
└── mcp-server/                       # as CAPACIDADES
    ├── scripts/
    │   └── bootstrap_login.py        # login manual (sync Playwright) → salva a sessão
    ├── src/azure_estimator_mcp/
    │   ├── server.py                 # FastMCP: registra as 10 tools; singleton do navegador
    │   ├── models.py                 # PriceResult, ServiceMatch, ServiceConfigSchema...
    │   └── azure/
    │       ├── retail_client.py      # Retail Prices API: paginação, retry, regiões
    │       ├── meters.py             # config → meter (um resolver por serviço)
    │       ├── pricing.py            # resolve_price, monthly_cost, sql_monthly_cost
    │       ├── catalog.py            # busca de serviços e schema de config
    │       ├── calculator_client.py  # driver Playwright da calculadora
    │       └── config_translate.py   # vocabulário do resolver → vocabulário da UI
    └── tests/                        # 265 testes (respx offline + integração + browser)
```

## Stack

| Camada              | Tecnologia            |
| ------------------- | --------------------- |
| Linguagem           | Python 3.14+          |
| Gerência de projeto | uv                    |
| MCP                 | SDK oficial (FastMCP) |
| HTTP (preço)        | httpx (async)         |
| Modelos             | pydantic v2           |
| Padrões             | PyYAML                |
| Navegador           | Playwright (Chromium) |
| Testes              | pytest, respx         |

## Testes

```bash
make test              # suíte offline (respx) — 265 testes
make test-integration  # bate na Retail Prices API real
make test-browser      # confere os seletores no DOM real da calculadora
                       # (não publica nada e não exige sessão)
```

`test_patterns_resolve.py` é o teste que amarra as duas metades do projeto: ele
lê os YAMLs de `patterns/` e vira **um caso por componente de cada padrão**, de
modo que uma falha aponta qual componente de qual padrão quebrou. Padrão novo na
Skill entra automaticamente na suíte.

## Armadilhas da Retail Prices API (leia antes de mexer nos resolvers)

O caro deste projeto não foi escrever os resolvers, foi descobrir onde a API
mente com cara de verdade. Cada item abaixo custou depuração e está travado por
teste — o detalhamento de cada um está no `CLAUDE.md`.

- **`isPrimaryMeterRegion` se inverte.** Filtrar pelo meter "primário" parece a
  desambiguação óbvia e devolve o meter **errado** em pelo menos dois casos: VM
  on-demand e AKS (onde o `Standard Uptime SLA` correto vem com `False`, e o
  add-on de suporte estendido, 6× mais caro, vem com `True`).
- **Região não comercial não é slug.** O `$filter` é case-sensitive e existem
  pseudo-regiões com maiúsculas e espaços significativos (`"Global"`,
  `"US Gov"`, `"Zone 1"`, nomes de continente). Slugificá-las devolve **lista
  vazia sem erro** — foi isso que escondeu Load Balancer, DNS e egress por
  semanas. Por isso `_SPECIAL_REGIONS` é uma allowlist, e cada entrada só entra
  depois de sondada na API real.
- **A licença do SQL ficou invisível por um mês.** Ela é um meter separado,
  cobrado por vCore/hora, com `armRegionName: "Global"` e a palavra "License" no
  `productName` (o `meterName` é só `"vCore"`). Cada busca isolada devolvia lista
  vazia — indistinguível de "não existe" — e a estimativa saía ~66% abaixo. Hoje
  `sql_monthly_cost` compõe compute + licença, e `licenseIncluded: false`
  corresponde a Azure Hybrid Benefit.
- **Nome de produto por substring erra em silêncio.** "Serverless" casa também
  com o Spark Pool do Synapse, cobrado por hora em vez de TB processado: o
  produto errado devolve um número plausível, não um erro. Daí a allowlist
  `SYNAPSE_TIERS` com `productName` exato.
- **Os defaults da calculadora contradizem os do resolver.** A UI vem com
  Windows, tier Hyperscale e — no SQL — reserva de 3 anos + Azure Hybrid
  Benefit, o que precifica ~53% abaixo do on-demand sem nada na tela dizendo
  isso. `config_translate.py` emite explicitamente tudo que a config determina,
  mesmo quando coincide com o default.

**A regra que atravessa tudo:** se um resolver não consegue isolar exatamente um
meter, ele levanta `PriceResolutionError` com os candidatos anexados. Nunca
chuta.

---

## Fases de desenvolvimento

| Fase | Descrição                                                                       |    Status     |
| :--: | ------------------------------------------------------------------------------- | :-----------: |
|  0   | Autenticação — spike do endpoint de save, Playwright, sessão persistida         | ✅ concluída  |
|  1   | Núcleo de preço — Retail Prices API, `config → meter`, `resolve_price` + testes | ✅ concluída  |
|  2   | MCP mínimo — as tools + fatia vertical (descrição → link) no Claude Code        | ✅ concluída  |
|  3   | Skill — biblioteca de padrões, interpretação e validação                        | ✅ concluída  |
|  4   | Integração e entrega — teste ponta a ponta, robustez, demo                      | ✅ concluída  |

**Princípio que atravessa tudo:** resolver o incerto antes do trabalhoso, e fechar uma fatia vertical de ponta a ponta antes de crescer em largura. O núcleo determinístico (Fases 0–1) é código testável que não depende do LLM; o agente e a Skill só entram na Fase 3, orquestrando por cima de ferramentas que já funcionam sozinhas.

### Cronograma executado (29/07 – 24/08)

Três blocos de trabalho, distribuídos para rodarem **em paralelo**: enquanto Samuel fechava o núcleo de preço, André adiantava o esqueleto do MCP (com tools _stub_, sem depender do preço pronto) e Natália construía a biblioteca de padrões e a Skill (conhecimento, independente do código).

#### Fase 1 — Núcleo de preço · 29/07 – 01/08

| Membro      | Tarefa                                                                                                            |
| ----------- | ----------------------------------------------------------------------------------------------------------------- |
| **Samuel**  | `retail_client.py` + `meters.py` (VM, Storage, SQL) + `resolve_price`, com testes pytest                          |
| **André**   | Esqueleto do servidor MCP: `server.py` (FastMCP) registrando as tools como _stubs_; validar no MCP Inspector      |
| **Natália** | Biblioteca de padrões: 3 arquiteturas de referência em YAML (three-tier, AKS, lakehouse) + rascunho do `SKILL.md` |

#### Fase 2 — MCP mínimo (fatia vertical) · 04/08 – 08/08

| Membro      | Tarefa                                                                                                                                                   |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Samuel**  | Ligar `resolve_price` às tools de preço (`search_azure_services`, `get_service_config_schema`, `resolve_price`, `estimate_monthly_cost`)                 |
| **André**   | Implementar `export_estimate` dirigindo a UI da calculadora via Playwright: mapear seletores (buscar serviço, configurar, compartilhar), capturar o link |
| **Natália** | Testar a fatia vertical no Claude Code (pedido cru → link + custo) e registrar bugs e lacunas de configuração                                            |

#### Fase 3 — Skill + interpretação · 10/08 – 21/08

| Membro      | Tarefa                                                                                            |
| ----------- | ------------------------------------------------------------------------------------------------- |
| **Natália** | Finalizar `SKILL.md` (workflow completo) + `interpretation-guide.md` + `validation-rules.md`      |
| **Samuel**  | Afinar os resolvers para garantir que todos os serviços dos 3 padrões resolvem preço corretamente |
| **André**   | Robustez do export: retry, fallback e detecção de sessão expirada (orienta a refazer o bootstrap) |

#### Fase 4 — Integração e entrega · 24/08

| Membro              | Tarefa                                                                                                 |
| ------------------- | ------------------------------------------------------------------------------------------------------ |
| **Todos**           | Teste ponta a ponta: arquiteto descreve uma arquitetura padrão → recebe link + custo; correção de bugs |
| **Samuel**          | Finalizar `README.md` e o `.mcp.json` de instalação                                                    |
| **André + Natália** | Preparar a demo da entrega: roteiro + caso de exemplo completo                                         |

## Equipe

- **Samuel** — núcleo de preço, integração das tools, documentação
- **André** — servidor MCP, automação do navegador (export)
- **Natália** — Skill, biblioteca de padrões, testes de ponta a ponta

## Segurança

- `.auth/storage_state.json` contém **credenciais de sessão reais** (cookies + CSRF) → está no `.gitignore`, nunca é commitado nem impresso em logs.
- Sem segredos hardcoded no código. Sem auto-refresh de sessão: renovar exige um login humano em um navegador real, por decisão de projeto.
- Tokens de sessão (JWT) capturados durante depuração **não** devem ser colados em issues, commits ou canais públicos — expiram sozinhos em poucas horas.
