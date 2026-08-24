# Guia de Interpretação — Casamento de Padrão e Extração de Componentes

> Este arquivo aprofunda as regras de interpretação citadas em `SKILL.md`. O `SKILL.md` define o procedimento (o que fazer, em que ordem); este arquivo define o critério (como decidir nos casos que o procedimento não cobre em detalhe). Em caso de conflito entre os dois, o `SKILL.md` vence, por ser o documento operacional.

## 1. Múltiplos padrões casando ao mesmo tempo

O casamento hoje é por substring simples, case-insensitive, contra `pattern.match.keywords`. Isso significa que, em teoria, mais de um padrão pode casar com a mesma descrição.

Regras, nessa ordem:

1. **Especificidade vence.** Se um padrão casa por mais keywords distintas que outro, use o mais específico. Duas keywords batendo ao mesmo tempo (ex.: "microsserviços" e "kubernetes" ambos presentes) é sinal mais forte que uma keyword isolada.
2. **Coerência com o resto da descrição.** Antes de aplicar o padrão casado, confira se os componentes dele fazem sentido dado o resto do pedido. Se a descrição contradiz um componente do padrão (ex.: "sem usar Kubernetes, só quero comparar" ao lado da palavra "kubernetes"), não aplique o padrão cegamente, trate como pedido não padrão.
3. **Empate real, pergunte.** Se dois padrões casam com força equivalente e a descrição não dá pista de qual o usuário quer, pergunte antes de escolher. Não assuma o primeiro da lista.

A lista de keywords de cada padrão vive em `patterns/*.yaml`, não neste arquivo, pelo mesmo motivo que o `SKILL.md` trata `get_service_config_schema` como fonte de verdade sobre tabelas estáticas: os YAMLs podem mudar, este texto não deveria precisar acompanhar cada edição.

## 2. Ajustar o padrão casado vs. cair no fallback

Casar com um padrão não significa que o pedido inteiro precisa vir dele sem alteração. A pergunta certa não é "o padrão casou?", é "o que exatamente está mudando?".

**Ajuste o padrão casado quando** a mudança pedida é um valor dentro de um componente que o padrão já define: região, SKU, tier, vCores, tamanho, quantidade. Dois exemplos reais, já testados: trocar o tier/vCores do componente `database` (General Purpose 2 vCores → Business Critical 16 vCores), e trocar a série de VM do componente `app-tier` (D2s_v3 → E2s_v3). Nos dois casos o papel do componente continua o mesmo, só o valor interno muda.

**Considere fallback, ao menos para aquele componente específico, quando** a mudança introduz um papel que o padrão não tem (ex.: pedir uma fila de mensagens numa arquitetura three-tier que não prevê isso), remove um componente que o padrão exige, ou muda a natureza do componente a ponto de o resolver do padrão não fazer mais sentido (ex.: trocar um banco relacional gerenciado por um não relacional).

Nesse segundo caso não descarte o padrão inteiro: mantenha os componentes que continuam válidos e trate só o componente alterado via fallback (passo 2 do `SKILL.md`).

## 3. Quando perguntar ao usuário vs. assumir um valor

Pergunte quando:

- Existem duas ou mais interpretações plausíveis que levam a estimativas materialmente diferentes, não apenas diferença de centavos.
- O histórico da conversa tem mais de um estado possível de "base" a partir do qual aplicar uma mudança nova, e não está claro qual estado usar. Exemplo real: depois de ajustar o banco para Business Critical numa mensagem, e pedir uma mudança de VM na mensagem seguinte, não estava claro se o banco alterado devia continuar valendo ou se a mudança nova partia do padrão original.
- Um campo obrigatório não tem default documentado em `SKILL.md` e o valor escolhido teria impacto significativo no custo total.

Não pergunte, assuma o default documentado, quando o campo tem default explícito na tabela de "Serviços suportados hoje" do `SKILL.md`, ou quando a escolha não muda o resultado de forma material.

## 4. Fallback: escolhendo entre múltiplos resultados de `search_azure_services`

O `SKILL.md` já define a regra básica: mais de um resultado, use o de maior score, a menos que a ambiguidade justifique perguntar. Critério prático para essa decisão:

- **Scores próximos entre os dois primeiros resultados**, sem um vencedor claro, é sinal de perguntar.
- **`service_family` dos resultados diverge de forma relevante** para o mesmo termo de busca (ex.: um resultado em Storage e outro em Databases para "fila") é sinal de perguntar, porque a escolha errada muda a arquitetura, não só o preço.
- Score isolado e claramente maior, ou um único resultado, siga direto sem perguntar.

**Decompondo um pedido com múltiplos serviços.** Quando o fallback precisa cobrir uma descrição com vários componentes numa frase só, trate cada papel arquitetural como uma busca separada antes de montar qualquer config. Primeiro identifique os papéis (ex.: "cluster de containers", "fila de mensagens", "cache"), depois rode uma chamada de `search_azure_services` por papel, isoladamente. Não tente casar a frase inteira contra o catálogo de uma vez, isso tende a devolver resultados genéricos demais para qualquer um dos papéis.

## 5. Defaults de nível de pedido

Antes dos defaults de campo dentro de um schema (`get_service_config_schema`), o pedido como um todo tem defaults próprios, aplicados quando o usuário não especifica:

| Aspecto | Default |
|---|---|
| Região | East US |
| Moeda | USD |
| Quantidade de instâncias, quando não vem de um padrão e não é especificada | 1 |

Esses defaults devem ser declarados na seção "Premissas assumidas" da resposta final, do mesmo jeito que os defaults de campo dentro de um schema.
