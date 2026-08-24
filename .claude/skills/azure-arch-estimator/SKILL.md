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

> Este arquivo descreve o fluxo completo: interpretar → resolver preço →
> montar a estimativa e, quando possível, gerar o link real da calculadora
> Azure via `create_estimate`/`add_line_item`/`export_estimate`
> (`calculator_client.py`, Playwright). Nem todo componente entra no link
> hoje — ver "Serviços com preço mas sem link" e "Limitações atuais" no
> fim do arquivo. Os arquivos
> `reference/interpretation-guide.md` e `reference/validation-rules.md`
> aprofundam as regras de casamento de padrão e validação; aqui fica o
> fluxo operacional, use os dois arquivos de referência quando precisar
> do critério por trás de uma decisão específica.

## O que essa Skill faz

Dado um pedido como "quero uma aplicação web com frontend, backend e banco
de dados", esta Skill guia o agente (Claude Code) a:

1. Tentar casar o pedido com um dos padrões em `patterns/` (determinístico,
   por palavra-chave).
2. Se nenhum padrão casar, descobrir os componentes via `search_azure_services`
   e `get_service_config_schema` em vez de adivinhar (ver passo 2 do fluxo) —
   hoje isso cobre os serviços com resolver implementado (`vm`, `storage`,
   `sql`, `aks`, `synapse`), então qualquer coisa fora dessas cinco continua
   sinalizada como bloqueada.
3. Para cada componente, chamar as tools do MCP `azure-pricing-estimator`
   para resolver preço e projetar custo mensal.
4. Somar os custos, apresentar a estimativa ao usuário em uma tabela e,
   quando possível, gerar o link real da calculadora Azure, deixando
   explícito quando algum componente não pôde ser precificado ou não
   entrou no link.

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

Antes de prosseguir para os próximos passos, declare explicitamente ao
usuário o resultado deste passo: qual padrão foi casado (se houver), ou
que nenhum padrão casou e por isso o fluxo seguiu para o passo 2
(fallback). Essa declaração deve aparecer na resposta final ao usuário,
não apenas no raciocínio interno.

> Para critérios mais profundos (múltiplos padrões casando ao mesmo
> tempo, quando ajustar o padrão casado versus tratar como fallback,
> quando perguntar ao usuário versus assumir um valor), veja
> `reference/interpretation-guide.md`.

### 2. Fallback — extrair componentes sem padrão

Quando não houver casamento por keyword, não adivinhe: use as tools de
descoberta do MCP para confirmar cada componente contra a API antes de
montar a config.

1. **Descobrir o serviço.** Para cada componente da descrição, chame:

   ```
   search_azure_services(query, currency="USD") -> list[ServiceMatch]
   ```

   Passe o termo mais literal possível (ex.: "kubernetes", "fila de
   mensagens", "banco de dados"). A tool devolve, ordenados por
   relevância, os serviços cujo `serviceName` foi confirmado por uma
   sonda real na API — cada `ServiceMatch` traz `key` (a chave que
   `resolve_price`/`get_service_config_schema` esperam), `service_name`
   exato, `label`, `service_family` e `matched_on` (o termo do catálogo
   que casou, útil para entender por que aquele serviço apareceu).

   - **Lista vazia** = a API não confirma nenhum serviço para esse termo
     dentro do catálogo hoje (que cobre `vm`, `storage`, `sql`, `aks` e
     `synapse` — ver "Serviços com preço mas sem link"). Trate como
     bloqueado e sinalize ao usuário; não invente uma `key` fora do que a
     tool devolveu.
   - **Um resultado** → use a `key` dele.
   - **Mais de um resultado** → use o primeiro (maior score); se a
     descrição do usuário for ambígua a ponto de você não ter confiança
     de qual serviço ele quer, pergunte antes de seguir em vez de
     chutar.

   > Critério mais detalhado de quando a ambiguidade justifica perguntar,
   > e como decompor uma descrição com vários componentes em buscas
   > separadas, está em `reference/interpretation-guide.md`.

2. **Descobrir os campos de config.** Com a `key` (ou o `service_name`)
   em mãos, chame:

   ```
   get_service_config_schema(service, region, currency="USD") -> ServiceConfigSchema
   ```

   Use a região que o usuário pediu, ou o default `East US` se ele não
   especificar (mesma regra do passo 4). A resposta já traz:

   - `fields`: lista de `FieldSchema` — nome, tipo, `required`,
     descrição, `default`, uma amostra de `values` válidos direto da
     API, `value_count` (total real de valores distintos) e
     `values_source` (a query que gerou a lista, para buscar o valor
     completo se precisar de algo fora da amostra);
   - `example_config`: uma config já pronta e aceita por `resolve_price`
     — use como ponto de partida em vez de montar do zero;
   - `notes`: avisos específicos do serviço que não cabem num campo
     isolado — ex.: variantes descartadas pelo resolver (Spot/Low
     Priority em VM), a unidade de `usage` esperada por
     `estimate_monthly_cost`, ou combinações que tendem a gerar
     `PriceResolutionError` (ex.: SQL sem `vCores` costuma sobrar mais
     de um meter). Leia antes de montar a config, não só `fields`/
     `example_config`.

3. **Montar a config final.** Parta do `example_config` e sobrescreva só
   o que o usuário efetivamente pediu (região, tamanho de VM, tier
   etc.). Para os campos obrigatórios que ele não mencionou, use o
   `default` do próprio `FieldSchema` quando existir.

   Se não houver default e o campo for obrigatório, verifique primeiro
   se o valor desejado aparece na amostra de `values`. Se aparecer, use
   normalmente.

   Se `values_truncated` for `true` e o valor desejado não estiver na
   amostra (comum em campos com `value_count` alto, como `armSkuName`,
   que tem 1792 valores possíveis contra uma amostra de menos de 25),
   não descarte a escolha só por isso. Existem duas formas de confirmar
   o valor antes de apresentá-lo como definitivo:

   - Tentar resolver o preço diretamente (`resolve_price` ou
     `estimate_monthly_cost`) com o valor escolhido. Se resolver sem
     `PriceResolutionError`, isso já confirma que o valor existe como
     meter real — informe ao usuário que a confirmação veio dessa forma.
     Se levantar `PriceResolutionError`, trate como no caso de "Erros
     esperados": não insista no mesmo valor, revise a escolha ou informe
     o usuário que aquele valor específico não pôde ser confirmado.
   - Alternativamente, montar uma consulta adicional via `values_source`
     antes de tentar o preço, útil quando o campo não afeta diretamente
     a resolução de preço (então uma falha na tentativa direta não
     serviria como sinal confiável).

   Em qualquer um dos casos, deixe a escolha explícita ao usuário como
   premissa assumida, e diga como ela foi confirmada — igual ao campo
   `notes` dos padrões.

> A tabela em "Serviços suportados hoje" abaixo continua útil como
> referência rápida offline, mas em runtime a fonte de verdade passa a
> ser `get_service_config_schema`, direto da API — se ela e a tabela
> divergirem, a tool vence.

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

- Multiplique o custo mensal de cada componente pelo seu `quantity` para a
  tabela que você apresenta. `resolve_price`/`estimate_monthly_cost`
  devolvem valor por unidade — é responsabilidade da Skill multiplicar e
  somar na hora de apresentar o resultado. Isso é independente do link da
  calculadora, onde `add_line_item` já trata `quantity` sozinho (ver
  abaixo).
- Monte uma tabela: componente, papel, serviço, quantidade, custo
  unitário/mês, custo total/mês, e o total geral.
- Deixe explícitas as premissas assumidas (região, tamanho de VM, volume de
  storage etc.), do mesmo jeito que o campo `notes` faz nos padrões. Liste
  essas premissas numa seção própria da resposta, por exemplo "Premissas
  assumidas:", com uma linha por componente, em vez de mencioná-las apenas
  como perguntas abertas ao final.
- Antes de gerar o link, confira `check_calculator_auth()`. Se vier
  `False`, a sessão expirou — informe isso ao usuário em vez de chamar
  `create_estimate`/`export_estimate`, que vão falhar com
  `CalculatorAuthError` de qualquer forma. A correção é rodar
  `bootstrap_login.py` de novo; não há renovação automática.
- Gere o link: `create_estimate()` uma vez, depois `add_line_item(service,
  config, usage, quantity)` para cada componente, e por fim
  `export_estimate()`. A resposta de `add_line_item` traz `assumptions`
  (campos da UI que ficaram no default) e `unsupported` (o que não pôde
  ser aplicado, ex.: `quantity` > 1 em `storage`, cujo painel não tem
  campo de quantidade de contas) — mostre as duas listas ao usuário.
- Componentes cujo `add_line_item` falhar com `ConfigTranslationError`
  (hoje, `aks` e `synapse` — ver "Serviços com preço mas sem link") entram
  no custo total apresentado, mas ficam de fora do link. Informe isso
  explicitamente.

> Checklist completo de autovalidação antes de apresentar a estimativa, e
> a regra sobre o que pode virar valor numérico na resposta para
> componentes bloqueados, estão em `reference/validation-rules.md`.

## Serviços suportados hoje

Estes têm resolver funcional em `meters.py` — são os que `resolve_price`
consegue de fato precificar:

| service | Campos de `config` | Obrigatórios | Defaults |
|---------|---------------------|--------------|----------|
| `vm` | `armSkuName`, `region`, `windows`, `priceType` | `armSkuName`, `region` | `windows=false` (Linux), `priceType=Consumption` |
| `storage` | `region`, `redundancy`, `tier`, `productName`, `priceType` | `region` | `redundancy=LRS`, `tier=Hot`, `productName=Blob Storage`, `priceType=Consumption` |
| `sql` | `region`, `tier`, `compute`, `hardware`, `vCores`, `priceType`, `licenseIncluded` | `region`, `vCores` (se `licenseIncluded=True`) | `tier=General Purpose`, `compute=Provisioned`, `hardware=Gen5`, `priceType=Consumption`, `licenseIncluded=true` |
| `aks` | `region`, `tier`, `longTermSupport`, `priceType` | `region` | `tier=Standard`, `longTermSupport=false`, `priceType=Consumption` |
| `synapse` | `region`, `tier`, `priceType` | `region` | `tier=Serverless SQL Pool`, `priceType=Consumption` |

`sql` agora compõe compute + licença automaticamente (`estimate_monthly_cost`
já devolve o total certo). `licenseIncluded=false` corresponde a Azure
Hybrid Benefit (BYOL), sem cobrança de licença.

Campos de `usage` esperados por `monthly_cost` (conforme o `unit_of_measure`
devolvido pela API — a Skill não escolhe isso, é o meter que dita):

| unit_of_measure | campo de `usage` | default |
|---|---|---|
| `1 Hour` | `hours` | 730 (mês cheio) |
| `1 GB` / `1 GB/Month` | `gb` | obrigatório, sem default |
| `1 TB` | `tbProcessed` (ou `tb`) | obrigatório, sem default |
| `1/Month` / `1 Month` | (nenhum) | custo fixo mensal |
| `1/Day` / `1 Day` | (nenhum) | projetado ao mês |

Qualquer outro `unit_of_measure` faz `monthly_cost` levantar erro — não é
para a Skill tentar mapear na mão; é sinal de que falta suporte em
`pricing.py`.

## Serviços com preço mas sem link na calculadora

- **`aks`** — resolve preço normalmente (SLA do control plane, componente
  `control-plane-sla` de `aks-microservices.yaml`), mas `config_translate.py`
  ainda não tem tradução de UI para ele. `add_line_item` recusa com
  `ConfigTranslationError`. Nós do cluster (node pool) não têm esse
  problema — são VMs normais, cobertas por `vm`.
- **`synapse`** — mesma situação: preço resolve (`synapse-serverless-sql`
  de `data-lakehouse.yaml`), mas sem tradução de UI ainda.

Ao montar uma estimativa que inclua um destes, informe claramente ao
usuário que o componente entra no custo total, mas fica de fora do link
da calculadora.

> Regras detalhadas de como apresentar isso ao usuário sem confundir com
> um componente sem preço nenhum estão em `reference/validation-rules.md`.

## Erros esperados

- `PriceResolutionError` — o resolver não conseguiu isolar exatamente um
  meter (zero candidatos, ou mais de um). Trate como um sinal para revisar
  a `config` do componente (região, SKU, tier etc.), não como algo para
  contornar adivinhando um valor.
- `ValueError` de `monthly_cost` — `unit_of_measure` do meter não tem
  mapeamento de uso conhecido. Reporte o componente como não estimável por
  enquanto.
- Serviço fora de `vm`/`storage`/`sql`/`aks`/`synapse` — trate como
  bloqueado (não tem resolver de preço).
- `ConfigTranslationError` em `add_line_item` — o componente tem preço mas
  não entra no link da calculadora ainda (ver "Serviços com preço mas sem
  link"). Não é erro de config, não tente contornar mudando valores.
- `CalculatorAuthError` — sessão expirada. Não insista em `create_estimate`/
  `export_estimate`; oriente o usuário a rodar `bootstrap_login.py`.

> Detalhamento de cada causa de `PriceResolutionError` e como confirmar
> valores fora da amostra truncada de um campo estão em
> `reference/validation-rules.md`.

## Limitações atuais

- `aks` e `synapse` resolvem preço mas não entram no link da calculadora
  ainda (`ConfigTranslationError` em `add_line_item`) — ver "Serviços com
  preço mas sem link".
- `quantity` dos componentes, no **custo total apresentado pela Skill**,
  não é somado automaticamente por `resolve_price`/`estimate_monthly_cost`
  (que devolvem valor por unidade) — a soma continua responsabilidade da
  Skill (passo 4). Isso é diferente do link da calculadora: ali
  `add_line_item` já traduz `quantity` para o campo de instâncias da UI
  sozinho, exceto em `storage`, onde o painel não tem esse campo.