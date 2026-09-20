"""Sync: parsers, idempotência e recusa em inventar URL."""

from __future__ import annotations

from dataclasses import replace

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
from anvisa_mcp.data.sync import _baixar_csv, _gravar

URL_FICTICIA = "https://exemplo-de-teste.invalido/medicamentos.csv"


# --- fontes -----------------------------------------------------------------


def test_fontes_confirmadas_apontam_para_a_anvisa() -> None:
    """URLs confirmadas em 2026-09-20 baixando os arquivos e lendo o cabeçalho."""
    for fonte in (MEDICAMENTOS, DISPOSITIVOS_MEDICOS):
        assert fonte.url is not None
        assert fonte.url.startswith("https://dados.anvisa.gov.br/dados/")
        assert fonte.confirmada_em == "2026-09-20"


def test_fonte_sem_url_para_em_vez_de_chutar_endpoint() -> None:
    """Se uma fonte perder a URL, o sync falha explicando — não inventa outra."""
    sem_url = replace(MEDICAMENTOS, url=None)
    with pytest.raises(FonteNaoConfigurada) as erro:
        sem_url.exigir_url()
    assert "dados.anvisa.gov.br" in str(erro.value)


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


# --- cabeçalhos reais da Anvisa (confirmados em 2026-09-20) ------------------


def test_parse_medicamentos_com_cabecalho_real() -> None:
    """Linha real de DADOS_ABERTOS_MEDICAMENTOS.csv."""
    linhas = [
        {
            "TIPO_PRODUTO": "MEDICAMENTO",
            "NOME_PRODUTO": "SANATOL",
            "DATA_FINALIZACAO_PROCESSO": "29/06/1979",
            "CATEGORIA_REGULATORIA": "Dinamizado",
            "NUMERO_REGISTRO_PRODUTO": "105760118",
            "DATA_VENCIMENTO_REGISTRO": "051998",
            "EMPRESA_DETENTORA_REGISTRO": "33379884000196 - LABORATORIO SIMOES LTDA.",
            "SITUACAO_REGISTRO": "Inativo",
            "PRINCIPIO_ATIVO": "smilax papiracea",
        }
    ]
    registro = parse_medicamentos(linhas)[0]
    assert registro["numero_registro"] == "105760118"
    assert registro["situacao"] == "Inativo"
    assert registro["principio_ativo"] == "smilax papiracea"
    # DATA_VENCIMENTO_REGISTRO vem como MMAAAA sem separador
    assert registro["data_situacao"] is not None
    assert registro["data_situacao"].isoformat() == "1998-05-01"


def test_parse_medicamentos_descarta_produto_notificado() -> None:
    """Produtos de baixo risco vêm sem número de registro e não entram."""
    linhas = [
        {
            "NOME_PRODUTO": "SUP GLI TESTE",
            "NUMERO_REGISTRO_PRODUTO": "",
            "SITUACAO_REGISTRO": "Ativo",
        }
    ]
    assert parse_medicamentos(linhas) == []


def test_parse_dispositivos_com_cabecalho_real() -> None:
    """Linha real de TA_PRODUTO_SAUDE_SITE.csv."""
    linhas = [
        {
            "NUMERO_REGISTRO_CADASTRO": "10330710009",
            "NOME_TECNICO": "Agulhas",
            "CLASSE_RISCO": "II",
            "NOME_COMERCIAL": "AGULHA PARA PUNÇÃO COM ACESSO PERCUTÂNEA RENAL",
            "DETENTOR_REGISTRO_CADASTRO": "HANDLE COMERCIO DE EQUIPAMENTOS MEDICOS LTDA",
            "NOME_FABRICANTE": "COOK INCORPORATED",
            "DT_PUB_REGISTRO_CADASTRO": "09/05/2000",
            "VALIDADE_REGISTRO_CADASTRO": "VIGENTE",
        }
    ]
    registro = parse_dispositivos(linhas)[0]
    assert registro["numero_registro"] == "10330710009"
    assert registro["classe_risco"] == "II"
    assert registro["situacao"] == "VIGENTE"
    assert registro["data_registro"] is not None
    assert registro["data_registro"].isoformat() == "2000-05-09"
    # sem campo de descrição livre no arquivo: o texto é montado dos nomes
    assert registro["descricao"] is not None
    assert "Nome técnico: Agulhas" in registro["descricao"]
    assert "COOK INCORPORATED" in registro["descricao"]


def test_parse_dispositivos_aceita_validade_como_data() -> None:
    """VALIDADE_REGISTRO_CADASTRO ora é 'VIGENTE', ora uma data."""
    linhas = [
        {
            "NUMERO_REGISTRO_CADASTRO": "81987060005",
            "NOME_COMERCIAL": "PARAFUSO",
            "CLASSE_RISCO": "III",
            "VALIDADE_REGISTRO_CADASTRO": "04/11/2034",
        }
    ]
    assert parse_dispositivos(linhas)[0]["situacao"] == "04/11/2034"


@respx.mock
async def test_baixar_csv_decodifica_iso8859() -> None:
    """A Anvisa publica em ISO-8859-1 sem declarar charset: acento tem que sobreviver."""
    corpo = "NUMERO_REGISTRO;NOME_PRODUTO\n1;PUNÇÃO ÓSSEA\n".encode("iso-8859-1")
    respx.get(URL_FICTICIA).mock(return_value=httpx.Response(200, content=corpo))
    linhas = await _baixar_csv(URL_FICTICIA)
    assert linhas[0]["NOME_PRODUTO"] == "PUNÇÃO ÓSSEA"


def test_gravar_deduplica_chave_repetida(db: duckdb.DuckDBPyConnection) -> None:
    """O mesmo registro aparece em várias linhas; DuckDB recusa atualizar duas vezes."""
    base = {
        "numero_registro": "1",
        "nome_produto": "APRESENTACAO A",
        "principio_ativo": None,
        "empresa_detentora": None,
        "situacao": "válido",
        "data_situacao": None,
        "categoria": None,
    }
    resultado = _gravar(db, "medicamentos", [base, {**base, "nome_produto": "APRESENTACAO B"}])
    assert resultado.total == 1
    assert db.execute("SELECT nome_produto FROM medicamentos").fetchone()[0] == "APRESENTACAO B"


def test_hora_local_e_validada() -> None:
    """A estratégia depende de horário fixo: formato inválido tem que falhar cedo."""
    from pydantic import ValidationError

    from anvisa_mcp.config import Config

    assert Config(sync_hora_local="03:20").sync_hora_local == "03:20"
    for invalido in ("25:00", "3:20", "03:60", "manha"):
        with pytest.raises(ValidationError):
            Config(sync_hora_local=invalido)
