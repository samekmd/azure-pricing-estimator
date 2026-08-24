# Regras de Validação — Config, Preço e Apresentação da Estimativa

> Este arquivo aprofunda as regras de validação citadas em `SKILL.md`, com exemplos reais observados em testes via Claude Code. Em caso de conflito entre os dois, o `SKILL.md` vence.

## 1. `PriceResolutionError` em detalhe

Esse erro tem duas causas bem diferentes, e a reação correta muda de acordo com qual delas ocorreu.

**Zero candidatos.** O resolver não achou nenhum meter que bata com a config. Normalmente sinal de um valor de config errado ou inexistente (SKU digitado errado, combinação de tier/hardware que não existe). Reação: revisite o valor específico contra a amostra de `values` ou uma consulta via `values_source`, não tente outro valor "parecido" sem confirmar.

**Mais de um candidato.** O resolver achou múltiplos meters que batem, e não consegue escolher sozinho. Normalmente sinal de um campo opcional que, nesse caso, deveria ter sido especificado. Exemplo documentado no `SKILL.md`: SQL sem `vCores` definido tende a sobrar mais de um meter. Reação: identifique qual campo está faltando para desambiguar, geralmente o campo que diferencia os candidatos retornados, e peça ou assuma um valor para ele, em vez de escolher arbitrariamente entre os candidatos.

## 2. Confirmando valores fora da amostra truncada

Caso de referência real: usuário pediu VM "série E ou M" para o `app-tier`. O agente escolheu `Standard_E2s_v3`, mantendo o mesmo número de vCPUs do padrão original (`Standard_D2s_v3`, 2 vCPU) como ponto de comparação justo. `armSkuName` tem `values_truncated: true` (amostra de cerca de 24 contra 1792 valores reais), e a série E não aparecia na amostra recebida.

O agente chamou `estimate_monthly_cost` direto com o valor escolhido. A chamada resolveu sem `PriceResolutionError`, o que confirma que o SKU existe como meter real na região, sem precisar de uma consulta adicional via `values_source`.

**Por que a tentativa direta funciona aqui, mas não sempre.** `armSkuName` é um campo que participa diretamente da resolução do preço, o resolver literalmente busca um meter que contenha esse valor. Uma resolução limpa é prova de que o valor existe. Isso não vale para todo campo: se um campo não participa diretamente da busca do meter, é usado só como metadado ou não afeta o filtro de busca, uma chamada de preço bem-sucedida não prova que aquele valor específico é válido, ela só prova que o resto da config resolveu. Nesse segundo caso a confirmação precisa vir de uma consulta explícita via `values_source`, não da tentativa direta.

Resumo prático: se o campo em questão é parte do filtro que o resolver usa para achar o meter (como `armSkuName`, `tier`, `vCores`), a tentativa direta confirma. Se o campo não entra nesse filtro, use `values_source`.

## 3. Checklist de autovalidação antes de apresentar a estimativa

Antes de mostrar a tabela final ao usuário, confira:

1. A soma dos subtotais bate matematicamente com o total apresentado.
2. Todo componente do padrão, ou do fallback, aparece na tabela, ou está explicitamente listado como bloqueado, com o motivo.
3. A declaração de qual padrão casou, ou que caiu em fallback, aparece antes da tabela, não só no raciocínio interno.
4. A seção "Premissas assumidas" está presente e cobre todo campo que recebeu um default sem pedido explícito do usuário.
5. O status do link está correto: se foi gerado, ele aparece; se algum componente ficou de fora por `ConfigTranslationError`, isso está sinalizado; se por sessão expirada ou erro só deu pra montar estimativa local, isso está dito explicitamente.
6. Nenhum valor de campo escolhido fora da amostra truncada é apresentado sem indicar como foi confirmado, ver seção 2.

## 4. O que pode virar valor numérico na resposta

Esta seção nasceu de um caso real: antes de `aks` ter resolver de preço, o agente incluiu, para o componente `control-plane-sla`, um valor aproximado (~$0,10/hora) vindo de conhecimento geral, não de nenhuma chamada ao MCP. Isso não pode mais acontecer nesse caso específico: `aks` agora tem resolver real (ver `SKILL.md`, "Serviços suportados hoje"), então esse preço sempre vem da API.

O princípio continua válido para qualquer serviço sem resolver: nenhum valor numérico apresentado na resposta pode vir de conhecimento geral do agente. Todo número precisa ser rastreável até uma chamada real ao MCP. Para componentes sem resolver, informe que não há preço disponível e pare por aí.

## 5. Tratamento consistente de serviço bloqueado

Caso de referência real: padrão `aks-microservices`, componente `control-plane-sla` (serviço `aks`, sem resolver em `meters.py`).

Regras:

- O bloqueio é por componente, não por padrão inteiro. `node-pool` (VMs normais) e `database` (SQL) do mesmo padrão continuam resolvíveis normalmente, mesmo com `control-plane-sla` bloqueado.
- Ao encontrar um serviço fora de `vm`/`storage`/`sql`, trate como bloqueio esperado, não como erro de execução, ver `SKILL.md`, seção "Erros esperados".
- A tabela final deve mostrar a linha do componente bloqueado explicitamente, com o motivo, em vez de simplesmente omitir a linha.
- O total apresentado deve deixar claro que exclui o componente bloqueado, por exemplo "Total estimado: $X/mês (sem contar o componente Y, ver abaixo)", nunca um total que pareça completo sem sê-lo.
