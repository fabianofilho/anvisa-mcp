"""Fixtures compartilhadas."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from anvisa_mcp.store.db import aplicar_schema


@pytest.fixture
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """DuckDB temporário com o schema aplicado."""
    conexao = duckdb.connect(str(tmp_path / "teste.duckdb"))
    aplicar_schema(conexao)
    yield conexao
    conexao.close()


@pytest.fixture
def caminho_db(tmp_path: Path) -> str:
    """Caminho de um DuckDB que ainda não existe (as tools criam)."""
    return str(tmp_path / "tools.duckdb")
