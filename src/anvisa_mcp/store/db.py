"""Conexão DuckDB, schema e migrations simples.

Uma tabela por dataset, mais uma tabela de cache das classificações de SaMD —
classificar de novo o mesmo registro custa uma chamada de LLM à toa.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

SCHEMA_VERSAO = 1

_DDL = """
CREATE TABLE IF NOT EXISTS medicamentos (
    numero_registro   VARCHAR PRIMARY KEY,
    nome_produto      VARCHAR NOT NULL,
    principio_ativo   VARCHAR,
    empresa_detentora VARCHAR,
    situacao          VARCHAR,
    data_situacao     DATE,
    categoria         VARCHAR,
    atualizado_em     TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS dispositivos_medicos (
    numero_registro   VARCHAR PRIMARY KEY,
    nome_produto      VARCHAR NOT NULL,
    empresa_detentora VARCHAR,
    classe_risco      VARCHAR,
    situacao          VARCHAR,
    data_registro     DATE,
    descricao         VARCHAR,
    atualizado_em     TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS classificacoes_samd (
    numero_registro VARCHAR PRIMARY KEY,
    usa_ia          BOOLEAN NOT NULL,
    confianca       DOUBLE NOT NULL,
    justificativa   VARCHAR NOT NULL,
    modelo          VARCHAR NOT NULL,
    classificado_em TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS schema_meta (
    chave VARCHAR PRIMARY KEY,
    valor VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_med_nome ON medicamentos (nome_produto);
CREATE INDEX IF NOT EXISTS idx_med_ativo ON medicamentos (principio_ativo);
CREATE INDEX IF NOT EXISTS idx_disp_classe ON dispositivos_medicos (classe_risco);
CREATE INDEX IF NOT EXISTS idx_disp_data ON dispositivos_medicos (data_registro);
"""


def aplicar_schema(conexao: duckdb.DuckDBPyConnection) -> None:
    """Cria tabelas e índices. Idempotente."""
    conexao.execute(_DDL)
    conexao.execute(
        "INSERT INTO schema_meta VALUES ('versao', ?) "
        "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
        [str(SCHEMA_VERSAO)],
    )


@contextmanager
def conectar(caminho: Path | str) -> Iterator[duckdb.DuckDBPyConnection]:
    """Abre o DuckDB no caminho dado, criando o diretório e o schema se preciso.

    ``:memory:`` é aceito e serve aos testes.
    """
    if str(caminho) != ":memory:":
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    conexao = duckdb.connect(str(caminho))
    try:
        aplicar_schema(conexao)
        yield conexao
    finally:
        conexao.close()
