"""CLI: classificar passa pelo clone e publicação, como o sync."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import httpx
import pytest
import respx
from typer.testing import CliRunner

from anvisa_mcp.cli import app
from anvisa_mcp.store.db import conectar
from anvisa_mcp.store.queries import gravar_classificacao

ENDPOINT = "http://llm-de-teste/v1"


@pytest.fixture
def ambiente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "anvisa.duckdb"
    monkeypatch.chdir(tmp_path)  # longe de qualquer .env
    monkeypatch.setenv("DUCKDB_PATH", str(base))
    monkeypatch.setenv("QWEN_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("QWEN_MODEL", "modelo-de-teste")
    monkeypatch.setenv("QWEN_MAX_TENTATIVAS", "1")
    return base


def _base_servida(caminho: Path, *, evidencia: bool | None = None, com_veredito: bool) -> None:
    with conectar(caminho) as conexao:
        conexao.execute(
            """
            INSERT INTO dispositivos_medicos
                (numero_registro, nome_produto, empresa_detentora, classe_risco,
                 situacao, data_registro, descricao)
            VALUES ('1', 'SOFTWARE X', 'E', 'III', 'VIGENTE', ?, 'software de laudo')
            """,
            [date.today() - timedelta(days=10)],
        )
        conexao.execute(
            "INSERT INTO medicamentos (numero_registro, nome_produto) VALUES ('9', 'REMEDIO')"
        )
        if com_veredito:
            gravar_classificacao(
                conexao,
                numero_registro="1",
                usa_ia=False,
                confianca=0.9,
                justificativa="veredito antigo",
                modelo="m",
                evidencia_confere=evidencia,
            )


def _llm_no_ar() -> respx.Route:
    respx.get(f"{ENDPOINT}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    return respx.post(f"{ENDPOINT}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"usa_ia": true, "confianca": 0.8, '
                            '"justificativa": "laudo por software", '
                            '"termos_citados": ["software"]}'
                        }
                    }
                ]
            },
        )
    )


def _vereditos(caminho: Path) -> list[tuple[object, ...]]:
    conexao = duckdb.connect(str(caminho), read_only=True)
    try:
        return conexao.execute(
            "SELECT numero_registro, justificativa, evidencia_confere FROM classificacoes_samd"
        ).fetchall()
    finally:
        conexao.close()


@respx.mock
def test_classificar_publicar_troca_a_base_com_leitor_aberto(ambiente: Path) -> None:
    """Com o connector lendo, classificar escreve na cópia e troca no fim."""
    _base_servida(ambiente, com_veredito=False)
    _llm_no_ar()
    leitor = duckdb.connect(str(ambiente), read_only=True)  # o connector
    try:
        resultado = CliRunner().invoke(app, ["classificar", "--publicar", "--dias", "30"])
    finally:
        leitor.close()

    assert resultado.exit_code == 0, resultado.output
    assert "classificados agora: 1" in resultado.output
    assert _vereditos(ambiente) == [("1", "laudo por software", True)]
    assert ambiente.with_name(ambiente.name + ".anterior").exists()


@respx.mock
def test_classificar_reclassificar_refaz_os_antigos(ambiente: Path) -> None:
    _base_servida(ambiente, com_veredito=True, evidencia=None)
    rota = _llm_no_ar()

    resultado = CliRunner().invoke(
        app, ["classificar", "--publicar", "--reclassificar", "--dias", "30"]
    )

    assert resultado.exit_code == 0, resultado.output
    assert rota.call_count == 1
    assert _vereditos(ambiente) == [("1", "laudo por software", True)]


@respx.mock
def test_classificar_sem_reclassificar_mantem_os_antigos(ambiente: Path) -> None:
    _base_servida(ambiente, com_veredito=True, evidencia=None)
    rota = _llm_no_ar()

    resultado = CliRunner().invoke(app, ["classificar", "--publicar", "--dias", "30"])

    assert resultado.exit_code == 0, resultado.output
    assert rota.call_count == 0
    assert "classificados agora: 0" in resultado.output
    assert _vereditos(ambiente) == [("1", "veredito antigo", None)]


def test_classificar_sem_base_falha_claro(ambiente: Path) -> None:
    resultado = CliRunner().invoke(app, ["classificar"])
    assert resultado.exit_code == 1
    assert "não existe ou está vazia" in resultado.output
