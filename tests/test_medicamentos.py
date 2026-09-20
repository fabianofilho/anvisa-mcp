"""Tool de medicamentos: busca, ambiguidade e queda para mock."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from anvisa_mcp.mcp_server.tools.medicamentos import consultar_status_medicamento
from anvisa_mcp.store.queries import buscar_medicamentos


def _inserir(conexao: duckdb.DuckDBPyConnection, **campos: object) -> None:
    conexao.execute(
        """
        INSERT INTO medicamentos
            (numero_registro, nome_produto, principio_ativo, empresa_detentora,
             situacao, data_situacao, categoria)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            campos["numero_registro"],
            campos["nome_produto"],
            campos.get("principio_ativo"),
            campos.get("empresa_detentora"),
            campos.get("situacao"),
            campos.get("data_situacao"),
            campos.get("categoria"),
        ],
    )


def test_busca_por_nome_comercial(db: duckdb.DuckDBPyConnection) -> None:
    _inserir(db, numero_registro="1", nome_produto="NOVALGINA", principio_ativo="dipirona")
    assert [r["nome_produto"] for r in buscar_medicamentos(db, "novalgina")] == ["NOVALGINA"]


def test_busca_por_principio_ativo(db: duckdb.DuckDBPyConnection) -> None:
    _inserir(db, numero_registro="1", nome_produto="NOVALGINA", principio_ativo="dipirona")
    assert len(buscar_medicamentos(db, "dipirona")) == 1


def test_busca_ignora_acento_e_caixa(db: duckdb.DuckDBPyConnection) -> None:
    """Grafias variam: 'sodica' precisa achar 'SÓDICA'."""
    _inserir(db, numero_registro="1", nome_produto="DIPIRONA SÓDICA", principio_ativo="dipirona")
    assert len(buscar_medicamentos(db, "sodica")) == 1


def test_ambiguidade_devolve_todos(db: duckdb.DuckDBPyConnection) -> None:
    """Dois produtos com o mesmo ativo: a tool não pode escolher por conta própria."""
    _inserir(db, numero_registro="1", nome_produto="DIPIRONA A", principio_ativo="dipirona")
    _inserir(db, numero_registro="2", nome_produto="DIPIRONA B", principio_ativo="dipirona")
    assert len(buscar_medicamentos(db, "dipirona")) == 2


def test_situacao_caducada_e_preservada(db: duckdb.DuckDBPyConnection) -> None:
    _inserir(
        db,
        numero_registro="1",
        nome_produto="ANTIGO",
        situacao="caducado",
        data_situacao=date(2021, 6, 30),
    )
    linha = buscar_medicamentos(db, "antigo")[0]
    assert linha["situacao"] == "caducado"
    assert linha["data_situacao"] == date(2021, 6, 30)


async def test_base_vazia_cai_para_mock_marcado(caminho_db: str) -> None:
    """Sem sync, responde mock — mas marcado como tal, com aviso."""
    resposta = await consultar_status_medicamento("dipirona", caminho_db=caminho_db)
    assert resposta.fonte == "mock"
    assert resposta.aviso is not None
    assert all("MOCK" in r.nome_produto for r in resposta.resultados)


async def test_base_com_dados_nao_usa_mock(caminho_db: str) -> None:
    from anvisa_mcp.store.db import conectar

    with conectar(caminho_db) as conexao:
        _inserir(conexao, numero_registro="9", nome_produto="REAL", principio_ativo="teste")

    resposta = await consultar_status_medicamento("real", caminho_db=caminho_db)
    assert resposta.fonte == "duckdb"
    assert resposta.aviso is None
    assert resposta.resultados[0].nome_produto == "REAL"


@pytest.mark.parametrize("termo", ["", "   "])
async def test_termo_vazio_nao_consulta(termo: str, caminho_db: str) -> None:
    resposta = await consultar_status_medicamento(termo, caminho_db=caminho_db)
    assert resposta.total == 0
    assert resposta.aviso is not None
