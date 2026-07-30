.PHONY: install test test-integration bootstrap-login

install:
	uv sync
	uv run playwright install chromium

test:
	uv run pytest

test-integration:
	uv run pytest -m integration

bootstrap-login:
	uv run python mcp-server/scripts/bootstrap_login.py
