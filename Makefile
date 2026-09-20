.PHONY: install test lint format typecheck check llm sync serve clean

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

check: lint typecheck test

llm:            ## confirma que o LLM local (ODS) responde
	uv run anvisa-cli llm

sync:           ## sync manual dos datasets (falha claro se a URL nao foi confirmada)
	uv run anvisa-cli sync

serve:          ## sobe o servidor MCP no stdio
	uv run anvisa-mcp

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
