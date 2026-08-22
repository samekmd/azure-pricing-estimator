Contexto: MCP de estimativa de custos Azure. Fase 3, trilha de PREÇO (Samuel). O núcleo de preço
(retail_client.py, meters.py, pricing.py) está pronto e com os bugs de região Global e unidade /Day
já corrigidos. A Skill (skill/azure-arch-estimator/) tem 3 padrões YAML em patterns/ e trata os
serviços aks e synapse como BLOQUEADOS de propósito. Você não viu a conversa; este é o contexto.

Objetivo: garantir, de forma sistemática e travada por teste, que os resolvers atuais (vm, storage,
sql) resolvem corretamente TODA config concreta que aparece nos 3 padrões — e que os serviços
bloqueados (aks, synapse) falham de forma LIMPA e ESTÁVEL, porque é esse erro que a Skill captura para
sinalizar "não estimável". NÃO implemente resolvers novos (aks/synapse permanecem bloqueados — isso é
Fase 4). Escopo: afinar o que existe, não aumentar largura.

PASSO 1 — Inventário dos padrões (não altere nada ainda):
- Leia os 3 YAMLs em skill/azure-arch-estimator/patterns/ (three-tier-web-app, aks-microservices,
  data-lakehouse).
- Extraia, para cada componente: id, service, e a config completa.
- Monte uma tabela dividindo em (a) componentes RESOLVÍVEIS hoje (service ∈ vm/storage/sql) e
  (b) componentes BLOQUEADOS (service ∈ aks/synapse).
- Me mostre essa tabela e o plano de teste ANTES de escrever qualquer código.

PASSO 2 — Validador pattern-driven (o núcleo desta tarefa):
- Crie mcp-server/tests/test_patterns_resolve.py que:
  - Carrega os 3 YAMLs de patterns/ (parametrização pytest por componente — cada componente é um caso
    de teste, para a falha apontar exatamente qual).
  - Para cada componente RESOLVÍVEL: passa a config EXATA do YAML para resolve_price() e afirma que
    ela isola EXATAMENTE UM meter (sem PriceResolutionError) e devolve unit_price > 0.
  - Para cada componente BLOQUEADO (aks/synapse): afirma que a tentativa de resolver levanta o erro
    ESPERADO e ESTÁVEL (o mesmo que a Skill captura). Trave a forma desse erro — se ele mudar, a Skill
    quebra silenciosamente, então o teste tem que pegar.
- Marque os casos que batem na API real com @pytest.mark.integration (puláveis offline). Onde der,
  cubra a lógica de seleção de meter com respx (mock) para rodar sem rede.

PASSO 3 — Afinar resolvers só onde o PASSO 2 acusar:
- Se algum config concreto dos padrões NÃO isolar exatamente um meter (0 ou >1 candidatos), ajuste o
  resolver correspondente em meters.py para desambiguar aquele caso — minimamente, sem quebrar os
  casos que já passam.
- Atenção especial aos casos ainda NÃO exercitados até hoje:
  - aks-microservices usa Standard_D4s_v3 no node-pool (só o D2s_v3 foi testado antes) — confirme que
    o D4s_v3 isola limpo.
  - Os 3 padrões usam volumes de storage diferentes (200/500/1000 GB) com Hot/LRS — confirme que cada
    config de storage resolve o meter certo (o volume não afeta a resolução, mas a combinação
    tier/redundancy/produto sim).
  - sql General Purpose / Provisioned / Gen5 / 2 vCores aparece em dois padrões — confirme consistência.
- Se um ajuste for necessário, ele vem COM o caso travado no teste. Não conserte sem teste.

RESTRIÇÕES:
- Não implemente aks nem synapse. Eles continuam bloqueados; o teste apenas trava o erro limpo deles.
- Só toque em meters.py (se preciso afinar) e no novo arquivo de teste. Não mexa em retail_client.py,
  pricing.py, catalog.py, server.py, calculator_client.py nem na Skill.
- Nenhuma dependência nova além de um parser YAML se ainda não houver (PyYAML) — se precisar adicionar,
  avise no relatório.
- Preserve todos os testes existentes passando.

ACEITE:
- test_patterns_resolve.py cobre TODOS os componentes dos 3 padrões (resolvíveis e bloqueados).
- `uv run pytest -m "not integration"` passa; com rede, os casos de integração também.
- Todo componente resolvível dos padrões isola exatamente um meter com preço plausível.
- aks e synapse levantam o erro esperado, e esse erro está travado por teste.
- Qualquer afinação em meters.py veio acompanhada do teste que trava o caso.

Ao final, atualize o PROGRESS.md registrando o que foi validado e qualquer resolver afinado. Mostre-me
o inventário do PASSO 1 antes de implementar.