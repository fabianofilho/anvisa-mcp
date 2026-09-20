"""Sync: parsers, idempotência e recusa em inventar URL."""

from __future__ import annotations

import duckdb
import httpx
import pytest
import respx

from anvisa_mcp.data.sources import (
    DISPOSITIVOS_MEDICOS,
    MEDICAMENTOS,
    FonteNaoConfigurada,
    parse_dispositivos,
    parse_medicamentos,
)
from anvisa_mcp.data.sync import _baixar_csv, _gravar, sync_medicamentos

URL_FICTICIA = "https://exemplo-de-teste.invalido/medicamentos.csv"


# --- fontes -----------------------------------------------------------------


def test_fontes_nao_tem_url_inventada() -> None:
    """Enquanto ninguém confirmar o dataset, a URL fica vazia — de propósito."""
    assert MEDICAMENTOS.url is None
    assert DISPOSITIVOS_MEDICOS.url is None


def test_exigir_url_explica_como_resolver() -> None:
    with pytest.raises(FonteNaoConfigurada) as erro:
        MEDICAMENTOS.exigir_url()
    assert "dados.gov.br" in str(erro.value)


async def test_sync_para_em_vez_de_chutar_endpoint(db: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(FonteNaoConfigurada):
        await sync_medicamentos(db)


# --- parsers ----------------------------------------------------------------


def test_parse_medicamentos_normaliza_data_brasileira() -> None:
    linhas = [
        {
            "NUMERO_REGISTRO": "1.0000.0001",
            "NOME_PRODUTO": "NOVALGINA",
            "PRINCIPIO_ATIVO": "dipirona",
            "SITUACAO": "válido",
            "DATA_SITUACAO": "31/12/2030",
        }
    ]
    registro = parse_medicamentos(linhas)[0]
    assert registro["data_situacao"].isoformat() == "2030-12-31"


def test_parse_aceita_cabecalhos_alternativos() -> None:
    """Os CSVs da Anvisa variam de cabeçalho entre publicações."""
    linhas = [{"REGISTRO": "1", "PRODUTO": "X", "SUBSTANCIA": "y"}]
    registro = parse_medicamentos(linhas)[0]
    assert registro["numero_registro"] == "1"
    assert registro["principio_ativo"] == "y"


def test_parse_descarta_linha_sem_chave() -> None:
    linhas = [{"NOME_PRODUTO": "SEM REGISTRO"}, {"NUMERO_REGISTRO": "1", "NOME_PRODUTO": "OK"}]
    assert [r["nome_produto"] for r in parse_medicamentos(linhas)] == ["OK"]


def test_parse_data_invalida_vira_none() -> None:
    linhas = [{"NUMERO_REGISTRO": "1", "NOME_PRODUTO": "X", "DATA_SITUACAO": "sem data"}]
    assert parse_medicamentos(linhas)[0]["data_situacao"] is None


def test_parse_dispositivos_le_classe_de_risco() -> None:
    linhas = [
        {
            "NUMERO_REGISTRO": "8.0001",
            "NOME_PRODUTO": "SOFTWARE X",
            "CLASSE_RISCO": "III",
            "DATA_REGISTRO": "01/03/2026",
            "DESCRICAO": "detecção automatizada",
        }
    ]
    registro = parse_dispositivos(linhas)[0]
    assert registro["classe_risco"] == "III"
    assert registro["descricao"] == "detecção automatizada"


# --- download e gravação ----------------------------------------------------


@respx.mock
async def test_baixar_csv_detecta_ponto_e_virgula() -> None:
    respx.get(URL_FICTICIA).mock(
        return_value=httpx.Response(200, text="NUMERO_REGISTRO;NOME_PRODUTO\n1;NOVALGINA\n")
    )
    linhas = await _baixar_csv(URL_FICTICIA)
    assert linhas[0]["NOME_PRODUTO"] == "NOVALGINA"


def test_gravar_e_idempotente(db: duckdb.DuckDBPyConnection) -> None:
    """Rodar o mesmo sync duas vezes não pode duplicar linha."""
    registros = [
        {
            "numero_registro": "1",
            "nome_produto": "NOVALGINA",
            "principio_ativo": "dipirona",
            "empresa_detentora": "X",
            "situacao": "válido",
            "data_situacao": None,
            "categoria": None,
        }
    ]
    primeiro = _gravar(db, "medicamentos", registros)
    segundo = _gravar(db, "medicamentos", registros)

    assert primeiro.novos == 1 and primeiro.atualizados == 0
    assert segundo.novos == 0 and segundo.atualizados == 1
    assert db.execute("SELECT count(*) FROM medicamentos").fetchone()[0] == 1


def test_gravar_atualiza_situacao_mudada(db: duckdb.DuckDBPyConnection) -> None:
    base = {
        "numero_registro": "1",
        "nome_produto": "NOVALGINA",
        "principio_ativo": None,
        "empresa_detentora": None,
        "situacao": "válido",
        "data_situacao": None,
        "categoria": None,
    }
    _gravar(db, "medicamentos", [base])
    _gravar(db, "medicamentos", [{**base, "situacao": "caducado"}])
    assert db.execute("SELECT situacao FROM medicamentos").fetchone()[0] == "caducado"


def test_gravar_lista_vazia_nao_quebra(db: duckdb.DuckDBPyConnection) -> None:
    assert _gravar(db, "medicamentos", []).total == 0
