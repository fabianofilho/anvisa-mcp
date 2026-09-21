"""Modo connector: sem LLM, sem escrita, troca atômica e limite de taxa."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import respx

from anvisa_mcp.mcp_server.limite import LimitadorPorOrigem, origem_da_requisicao
from anvisa_mcp.mcp_server.tools.samd import buscar_samd_recentes
from anvisa_mcp.store.db import aplicar_schema, conectar
from anvisa_mcp.store.queries import gravar_classificacao
from anvisa_mcp.store.troca import (
    BaseSuspeita,
    caminho_em_construcao,
    clonar_para_construcao,
    publicar,
    reverter,
)

ENDPOINT = "http://llm-de-teste/v1"
MODELO = "modelo-de-teste"


def _dispositivo(conexao: duckdb.DuckDBPyConnection, numero: str, nome: str) -> None:
    conexao.execute(
        """
        INSERT INTO dispositivos_medicos
            (numero_registro, nome_produto, empresa_detentora, classe_risco,
             situacao, data_registro, descricao)
        VALUES (?, ?, 'EMPRESA', 'III', 'válido', ?, 'Software de detecção')
        """,
        [numero, nome, date.today() - timedelta(days=5)],
    )


# --- o servidor não chama o LLM ------------------------------------------


@respx.mock
async def test_modo_connector_nao_chama_o_llm(caminho_db: str) -> None:
    """Quem hospeda não paga inferência por todo mundo. Nem uma requisição sai."""
    rota_models = respx.get(f"{ENDPOINT}/models")
    rota_chat = respx.post(f"{ENDPOINT}/chat/completions")
    with conectar(caminho_db) as conexao:
        _dispositivo(conexao, "8.1", "CAD4TB")

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
        permitir_llm=False,
    )
    assert rota_models.call_count == 0
    assert rota_chat.call_count == 0
    assert resposta.resultados[0].classificacao.origem == "nao_classificado"


async def test_modo_connector_serve_o_cache(caminho_db: str) -> None:
    """O cache é construído na coleta, fora do servidor, e é o que ele serve."""
    with conectar(caminho_db) as conexao:
        _dispositivo(conexao, "8.1", "CAD4TB")
        gravar_classificacao(
            conexao,
            numero_registro="8.1",
            usa_ia=True,
            confianca=0.6,
            justificativa="nome sugere CAD",
            modelo=MODELO,
        )

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=True,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
        permitir_llm=False,
    )
    assert resposta.total == 1
    assert resposta.resultados[0].classificacao.origem == "cache"


async def test_nao_classificado_nao_vira_sem_ia(caminho_db: str) -> None:
    """'Não avaliado' é diferente de 'não usa IA', precisa entrar em indeterminados."""
    with conectar(caminho_db) as conexao:
        _dispositivo(conexao, "8.1", "DESCONHECIDO")

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=True,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
        permitir_llm=False,
    )
    assert resposta.total == 0
    assert resposta.indeterminados == 1
    assert resposta.aviso is not None and "não classifica sob demanda" in resposta.aviso


async def test_modo_connector_nao_escreve_na_base(caminho_db: str) -> None:
    """O DuckDB recusa escritor com leitor aberto: o servidor não pode gravar cache."""
    with conectar(caminho_db) as conexao:
        _dispositivo(conexao, "8.1", "CAD4TB")

    await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
        permitir_llm=False,
    )
    with conectar(caminho_db, somente_leitura=True) as conexao:
        linha = conexao.execute("SELECT count(*) FROM classificacoes_samd").fetchone()
    assert linha is not None and linha[0] == 0


# --- troca atômica --------------------------------------------------------


def _base_com(caminho: Path, quantos: int) -> None:
    conexao = duckdb.connect(str(caminho))
    aplicar_schema(conexao)
    conexao.executemany(
        "INSERT INTO medicamentos (numero_registro, nome_produto) VALUES (?, ?)",
        [[str(i), f"REMEDIO {i}"] for i in range(quantos)],
    )
    conexao.close()


def test_troca_funciona_com_leitor_aberto(tmp_path: Path) -> None:
    servida = tmp_path / "anvisa.duckdb"
    _base_com(servida, 100)
    _base_com(caminho_em_construcao(servida), 100)

    leitor = duckdb.connect(str(servida), read_only=True)
    try:
        publicar(servida)
    finally:
        leitor.close()
    assert servida.exists()


def test_recusa_publicar_base_que_encolheu(tmp_path: Path) -> None:
    servida = tmp_path / "anvisa.duckdb"
    _base_com(servida, 2000)
    _base_com(caminho_em_construcao(servida), 20)

    with pytest.raises(BaseSuspeita):
        publicar(servida)
    conexao = duckdb.connect(str(servida), read_only=True)
    assert conexao.execute("SELECT count(*) FROM medicamentos").fetchone()[0] == 2000
    conexao.close()


def test_reverter_volta_a_anterior(tmp_path: Path) -> None:
    servida = tmp_path / "anvisa.duckdb"
    _base_com(servida, 100)
    _base_com(caminho_em_construcao(servida), 95)
    publicar(servida)
    reverter(servida)

    conexao = duckdb.connect(str(servida), read_only=True)
    assert conexao.execute("SELECT count(*) FROM medicamentos").fetchone()[0] == 100
    conexao.close()


# --- limite de taxa -------------------------------------------------------


def test_teto_global_protege_independente_da_origem() -> None:
    limitador = LimitadorPorOrigem(limite_por_minuto=100, limite_global_por_minuto=3)
    assert all(limitador.permitir(f"10.0.0.{i}") for i in range(3))
    assert not limitador.permitir("10.0.0.99")
    assert limitador.motivo_ultima_recusa == "global"


def test_origem_usa_forwarded_for_quando_ha_proxy() -> None:
    scope: dict[str, Any] = {
        "headers": [(b"x-forwarded-for", b"203.0.113.9, 10.0.0.1")],
        "client": ("10.0.0.1", 5000),
    }
    assert origem_da_requisicao(scope) == "203.0.113.9"


def test_clone_preserva_o_que_nao_vem_do_dataset(tmp_path: Path) -> None:
    """A base nova nasce da servida: o cache caro e os sumidos continuam lá."""
    servida = tmp_path / "base.duckdb"
    with conectar(servida) as conexao:
        aplicar_schema(conexao)
        conexao.execute(
            "INSERT INTO classificacoes_samd "
            "(numero_registro, usa_ia, confianca, justificativa, modelo) "
            "VALUES ('123', true, 0.9, 'menciona rede neural', 'local-model')"
        )
        conexao.execute(
            "INSERT INTO dispositivos_medicos (numero_registro, nome_produto) "
            "VALUES ('sumiu-do-csv', 'Produto que saiu do dataset')"
        )

    nova = clonar_para_construcao(servida)

    with conectar(nova, somente_leitura=True) as conexao:
        assert conexao.execute("SELECT count(*) FROM classificacoes_samd").fetchone()[0] == 1
        assert (
            conexao.execute(
                "SELECT count(*) FROM dispositivos_medicos WHERE numero_registro = 'sumiu-do-csv'"
            ).fetchone()[0]
            == 1
        )


def test_clone_sem_base_servida_comeca_vazio(tmp_path: Path) -> None:
    """Primeira coleta da vida: não há de onde herdar, e isso não é erro."""
    destino = tmp_path / "ainda-nao-existe.duckdb"
    nova = clonar_para_construcao(destino)
    assert not nova.exists()
