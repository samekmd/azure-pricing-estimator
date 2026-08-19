---
name: azure-arch-estimator
description: >
  Interpreta a descrição em linguagem natural de uma arquitetura Azure,
  casa com um padrão de referência conhecido (ou extrai os componentes
  quando não há padrão), resolve o preço de cada componente via MCP e
  monta uma estimativa de custo mensal. Use quando o usuário descrever uma
  arquitetura (ex.: "aplicação web com banco de dados", "cluster kubernetes
  com storage") e quiser saber o custo estimado no Azure.
---

# Azure Architecture Estimator

> RASCUNHO (Fase 1/2). Este arquivo cobre o fluxo mínimo hoje possível:
> interpretar → resolver preço → montar estimativa local. A geração do link
> real da calculadora (`create_estimate` / `add_line_item` / `export_estimate`)
> ainda é stub — ver "Limitações atuais" no fim do arquivo. Os arquivos
> `reference/interpretation-guide.md` e `reference/validation-rules.md`
> (Fase 3) vão aprofundar as regras de casamento de padrão e validação;
> aqui fica só o essencial para já testar a fatia vertical.

## O que essa Skill faz

Dado um pedido como "quero uma aplicação web com frontend, backend e banco
de dados", esta Skill guia o agente (Claude Code) a:

1. Tentar casar o pedido com um dos padrões em `patterns/` (determinístico,
   por palavra-chave).
2. Se nenhum padrão casar, extrair os componentes da descrição por conta
   própria (raciocínio do agente), mas **só usando os serviços que já têm
   resolver implementado** (ver tabela abaixo) — não inventar componentes
   fora do vocabulário conhecido.
3. Para cada componente, chamar as tools do MCP `azure-pricing-estimator`
   para resolver preço e projetar custo mensal.
4. Somar os custos e apresentar a estimativa ao usuário em uma tabela,
   deixando explícito quando algum componente não pôde ser precificado.

## Fluxo de trabalho

### 1. Casar com um padrão

Percorra `patterns/*.yaml` e compare `pattern.match.keywords` com a
descrição do usuário (case-insensitive, substring simples). Se uma ou mais
keywords de um padrão aparecerem no pedido, use aquele padrão como ponto de
partida — ele já vem com `components` prontos (service, config, usage,
quantity).

Se o usuário mencionar uma variação (ex.: outra região, outro tamanho de
VM), ajuste os campos de `config`/`usage` do padrão casado em vez de
começar do zero — os padrões são um ponto de partida editável, não um
template rígido.

Se nenhum padrão casar, vá para o passo 2 (fallback).

### 2. Fallback — extrair componentes sem padrão

Quando não houver casamento por keyword, raciocine sobre a descrição do
usuário e monte uma lista de componentes no mesmo formato usado pelos
padrões (`service`, `config`, `usage`, `quantity`). Regras:

- Só use `service` dentre os que têm resolver implementado hoje: `vm`,
  `storage`, `sql` (ver tabela abaixo). Se a descrição exigir um serviço
  sem resolver (ex.: AKS, Synapse — ver "Serviços bloqueados"), sinalize
  isso ao usuário em vez de tentar adivinhar uma config.
- Preencha os campos obrigatórios de cada serviço (ver tabela); campos
  opcionais podem usar os defaults do próprio resolver (documentados
  abaixo) em vez de inventar valores.
- Quando a descrição não especificar volume/região/tamanho, adote os
  mesmos defaults usados pelos padrões de referência (região `East US`,
  já validada em `test_resolve_integration.py`) e deixe isso explícito
  para o usuário como premissa assumida — igual ao campo `notes` dos
  padrões.

### 3. Resolver preço de cada componente

Para cada componente (do padrão casado ou do fallback), chame a tool MCP:

```
resolve_price(service, config, currency="USD") -> PriceResult
```

ou, quando já quiser o custo mensal projetado direto:

```
estimate_monthly_cost(service, config, usage, currency="USD") -> float
```

Se a chamada levantar erro de resolução (preço ambíguo ou serviço
desconhecido), **não tente adivinhar** — reporte ao usuário qual
componente não pôde ser resolvido e por quê (ver "Erros esperados").

### 4. Somar e apresentar a estimativa

- Multiplique o custo mensal de cada componente pelo seu `quantity`. **Isso
  ainda não é feito automaticamente pelo MCP** (`add_line_item` é stub) —
  é responsabilidade da Skill somar na hora de apresentar o resultado.
- Monte uma tabela: componente, papel, serviço, quantidade, custo
  unitário/mês, custo total/mês, e o total geral.
- Deixe explícitas as premissas assumidas (região, tamanho de VM, volume de
  storage etc.), do mesmo jeito que o campo `notes` faz nos padrões.
- Informe que o resultado é uma **estimativa local**, não ainda um link da
  calculadora Azure (isso depende da Fase 2 do André — ver limitações).

## Serviços suportados hoje

Só estes três têm resolver funcional em `meters.py` — são os únicos que
`resolve_price` consegue de fato precificar:

| service | Campos de `config` | Obrigatórios | Defaults |
|---------|---------------------|--------------|----------|
| `vm` | `armSkuName`, `region`, `windows`, `priceType` | `armSkuName`, `region` | `windows=false` (Linux), `priceType=Consumption` |
| `storage` | `region`, `redundancy`, `tier`, `productName`, `priceType` | `region` | `redundancy=LRS`, `tier=Hot`, `productName=Blob Storage`, `priceType=Consumption` |
| `sql` | `region`, `tier`, `compute`, `hardware`, `vCores`, `priceType` | `region` | `tier=General Purpose`, `compute=Provisioned`, `hardware=Gen5`, `priceType=Consumption` |

Campos de `usage` esperados por `monthly_cost` (conforme o `unit_of_measure`
devolvido pela API — a Skill não escolhe isso, é o meter que dita):

| unit_of_measure | campo de `usage` | default |
|---|---|---|
| `1 Hour` | `hours` | 730 (mês cheio) |
| `1 GB` / `1 GB/Month` | `gb` | obrigatório, sem default |
| `1/Month` / `1 Month` | (nenhum) | custo fixo mensal |

Qualquer outro `unit_of_measure` faz `monthly_cost` levantar erro — não é
para a Skill tentar mapear na mão; é sinal de que falta suporte em
`pricing.py`.

## Serviços bloqueados (sem resolver ainda)

- **`aks`** — SLA do control plane gerenciado do AKS. Usado no componente
  `control-plane-sla` de `aks-microservices.yaml`. Nós do cluster (node
  pool) **não** são bloqueados — são VMs normais, já cobertos por `vm`.
- **`synapse`** — Azure Synapse Analytics (serverless SQL pool), usado no
  componente `synapse-serverless-sql` de `data-lakehouse.yaml`. Cobrado por
  TB processado, não por hora — quando o resolver existir, `usage` desse
  componente provavelmente precisará de um campo tipo `tbProcessed` (ainda
  não suportado por `monthly_cost`).

Ao montar uma estimativa que inclua um destes, informe claramente ao
usuário que aquele componente específico não tem preço disponível ainda,
mas siga precificando o restante da arquitetura normalmente.

## Erros esperados

- `PriceResolutionError` — o resolver não conseguiu isolar exatamente um
  meter (zero candidatos, ou mais de um). Trate como um sinal para revisar
  a `config` do componente (região, SKU, tier etc.), não como algo para
  contornar adivinhando um valor.
- `ValueError` de `monthly_cost` — `unit_of_measure` do meter não tem
  mapeamento de uso conhecido. Reporte o componente como não estimável por
  enquanto.
- Serviço fora de `vm`/`storage`/`sql` — trate como bloqueado (ver seção
  acima), não como erro de config.

## Limitações atuais

- `export_estimate`, `create_estimate` e `add_line_item` ainda são stubs no
  MCP (Fase 2 do André, dependem de `calculator_client.py` real via
  Playwright) — hoje a Skill só consegue montar uma **estimativa local**
  (soma de custos calculados), não gerar o link oficial da calculadora.
- `quantity` dos componentes não é somado automaticamente por nenhuma tool
  — a soma é feita pela Skill na hora de apresentar o resultado (passo 4).
- Serviços `aks` e `synapse` não têm resolver — ver seção acima.
- `interpretation-guide.md` e `validation-rules.md` (Fase 3) ainda não
  existem; até lá, este arquivo concentra as regras mínimas de
  interpretação e validação.
