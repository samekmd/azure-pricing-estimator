Contexto: estou construindo um MCP que gera links da calculadora de preços da Azure.
O endpoint de save da calculadora exige uma sessão de navegador autenticada (cookie +
token CSRF) e NÃO aceita token OAuth programático — já testei e confirmei (AADSTS65002).
Por isso vou usar Playwright: um navegador real, logado, que satisfaz a autenticação do
jeito que a Microsoft desenhou. Você não viu a conversa anterior; este é o contexto necessário.

Objetivo (esqueleto de autenticação): criar a base do cliente em dois arquivos. NÃO
implemente ainda a montagem da estimativa nem a captura do link — só a autenticação.

ARQUIVO 1 — mcp-server/scripts/bootstrap_login.py (script standalone, interativo, rodado uma vez):
- Usa a API SÍNCRONA do Playwright (one-shot com humano no meio, não precisa de event loop).
- Abre o Chromium em modo HEADED (headless=False), com um contexto novo (sem estado).
- Navega para https://azure.microsoft.com/en-us/pricing/calculator/
- Imprime instruções no terminal e PAUSA com um input() bloqueante esperando eu fazer login
  manualmente (incluindo MFA): "Pressione Enter depois de concluir o login e ver a calculadora logada."
- Após o Enter, salva o storage_state em .auth/storage_state.json (cria o diretório se preciso).
- Fecha o navegador e imprime onde salvou.

ARQUIVO 2 — mcp-server/src/azure_estimator_mcp/azure/calculator_client.py (o cliente do MCP):
- Usa a API ASSÍNCRONA do Playwright (vai ser chamado por tools async do MCP; a API síncrona
  não roda dentro de um event loop asyncio).
- Classe AzureCalculatorClient com:
  - __init__(self, storage_state_path=".auth/storage_state.json", headless=True)
  - Context manager async (__aenter__/__aexit__) que lança o Chromium e cria o contexto
    carregando o storage_state. Se o arquivo não existir, levanta erro claro pedindo para
    rodar o bootstrap_login.py primeiro.
  - async is_authenticated(self) -> bool: navega para a calculadora e detecta se a sessão
    está logada (ex.: presença de elemento de conta / ausência do botão "Sign in"). Deixe um
    TODO dizendo que o seletor exato precisa ser ajustado contra a página real; implemente
    uma verificação best-effort por enquanto.
  - async create_estimate(self), async add_line_item(self, service, config),
    async export_estimate(self) -> str: STUBS — levantem NotImplementedError com um TODO
    referenciando a Fase 2 (dirigir a UI e clicar em compartilhar). Não implemente a lógica agora;
    ainda não mapeamos os seletores da UI.
  - Fechamento/limpeza adequados no __aexit__.

Restrições:
- Playwright para Python. Adicione playwright ao pyproject.toml e lembre no final que preciso
  rodar `playwright install chromium`.
- NÃO faça auto-refresh de sessão. Se a sessão expirou, is_authenticated retorna False e o
  cliente orienta a rodar o bootstrap de novo. O ponto de usar navegador é delegar a auth a ele.
- Nada de segredos hardcoded.

Segurança (importante):
- .auth/storage_state.json contém credenciais de sessão reais. Adicione .auth/ ao .gitignore
  (crie o .gitignore se não existir). Nunca faça commit dele nem imprima o conteúdo em logs.

Critérios de aceite:
- `python mcp-server/scripts/bootstrap_login.py` abre um navegador visível, espera meu login e
  cria .auth/storage_state.json.
- Consigo instanciar AzureCalculatorClient como context manager async, chamar is_authenticated()
  e receber True com sessão válida (e False depois de apagar o estado).
- Os stubs levantam NotImplementedError com mensagens claras.
- .auth/ está no .gitignore.

Antes de escrever, me mostre a estrutura de arquivos que vai criar e confirme o plano. Depois implemente.