.PHONY: install test test-integration test-browser bootstrap-login

install:
	uv sync
	uv run playwright install chromium

test:
	uv run pytest

test-integration:
	uv run pytest -m integration

# Confere os seletores contra o DOM real da calculadora. Não publica nada e
# não exige sessão autenticada — só Chromium instalado.
test-browser:
	uv run pytest -m browser

bootstrap-login:
	uv run python mcp-server/scripts/bootstrap_login.py
