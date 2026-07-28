"""Bootstrap de login (roda UMA vez, com humano no meio).

Abre um Chromium visível para você fazer login manualmente (incluindo MFA) na
calculadora de preços da Azure e salva o estado de sessão (cookies + storage)
em .auth/storage_state.json. Esse estado é depois reutilizado pelo
AzureCalculatorClient para dirigir a UI já autenticado.

Uso:
    uv run python mcp-server/scripts/bootstrap_login.py

Observação de segurança: o arquivo gerado contém credenciais de sessão reais.
Ele está no .gitignore e NUNCA deve ser commitado nem impresso em logs.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

CALCULATOR_URL = "https://azure.microsoft.com/en-us/pricing/calculator/"
STORAGE_STATE_PATH = Path(".auth/storage_state.json")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        # Contexto novo, sem estado — você começa deslogado e autentica na mão.
        context = browser.new_context()
        page = context.new_page()
        page.goto(CALCULATOR_URL)

        print("=" * 70)
        print("LOGIN MANUAL NECESSÁRIO")
        print("-" * 70)
        print("1. Na janela do Chromium que abriu, faça login na sua conta")
        print("   Microsoft (inclusive MFA, se pedir).")
        print("2. Aguarde a calculadora carregar já autenticado.")
        print("=" * 70)
        input("Pressione Enter depois de concluir o login e ver a calculadora logada. ")

        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STORAGE_STATE_PATH))

        context.close()
        browser.close()

    print(f"\nEstado de sessão salvo em: {STORAGE_STATE_PATH.resolve()}")
    print("Não commite nem compartilhe esse arquivo — ele contém sua sessão.")


if __name__ == "__main__":
    main()
