"""Tool de SaMD: classificação, cache e degradação quando o LLM cai."""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import httpx
import pytest
import respx

from anvisa_mcp.llm.qwen_client import ClassificationResult, QwenClient, QwenIndisponivel
from anvisa_mcp.mcp_server.tools.samd import buscar_samd_recentes
from anvisa_mcp.store.db import conectar
from anvisa_mcp.store.queries import (
    classificacao_em_cache,
    dispositivos_no_periodo,
    gravar_classificacao,
)

ENDPOINT = "http://llm-de-teste/v1"
MODELO = "modelo-de-teste"


def _resposta_chat(conteudo: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": conteudo}}]})


def _inserir_dispositivo(
    conexao: duckdb.DuckDBPyConnection,
    *,
    numero: str,
    nome: str,
    classe: str = "III",
    dias_atras: int = 1,
    descricao: str = "",
) -> None:
    conexao.execute(
        """
        INSERT INTO dispositivos_medicos
            (numero_registro, nome_produto, empresa_detentora, classe_risco,
             situacao, data_registro, descricao)
        VALUES (?, ?, 'EMPRESA', ?, 'válido', ?, ?)
        """,
        [numero, nome, classe, date.today() - timedelta(days=dias_atras), descricao],
    )


# --- camada de armazenamento ------------------------------------------------


def test_periodo_exclui_registro_antigo(db: duckdb.DuckDBPyConnection) -> None:
    _inserir_dispositivo(db, numero="1", nome="NOVO", dias_atras=10)
    _inserir_dispositivo(db, numero="2", nome="ANTIGO", dias_atras=400)
    achados = dispositivos_no_periodo(db, dias=90)
    assert [d["nome_produto"] for d in achados] == ["NOVO"]


def test_periodo_filtra_classe_de_risco(db: duckdb.DuckDBPyConnection) -> None:
    """SaMD se concentra em III/IV: Classe I não entra."""
    _inserir_dispositivo(db, numero="1", nome="CLASSE I", classe="I")
    _inserir_dispositivo(db, numero="2", nome="CLASSE IV", classe="IV")
    assert [d["nome_produto"] for d in dispositivos_no_periodo(db, dias=90)] == ["CLASSE IV"]


def test_cache_de_classificacao_sobrescreve(db: duckdb.DuckDBPyConnection) -> None:
    gravar_classificacao(
        db,
        numero_registro="1",
        usa_ia=False,
        confianca=0.4,
        justificativa="texto vago",
        modelo=MODELO,
    )
    gravar_classificacao(
        db,
        numero_registro="1",
        usa_ia=True,
        confianca=0.9,
        justificativa="declara rede neural",
        modelo=MODELO,
    )
    cacheado = classificacao_em_cache(db, "1")
    assert cacheado is not None
    assert cacheado["usa_ia"] is True
    assert cacheado["confianca"] == pytest.approx(0.9)


# --- cliente do LLM ---------------------------------------------------------


@respx.mock
async def test_classify_aceita_json_puro() -> None:
    respx.post(f"{ENDPOINT}/chat/completions").mock(
        return_value=_resposta_chat(
            '{"usa_ia": true, "confianca": 0.9, "justificativa": "rede neural"}'
        )
    )
    async with QwenClient(ENDPOINT, MODELO) as cliente:
        resultado = await cliente.classify("texto", "prompt")
    assert resultado == ClassificationResult(
        usa_ia=True, confianca=0.9, justificativa="rede neural"
    )


@respx.mock
async def test_classify_tolera_json_embrulhado_em_prosa() -> None:
    """Modelo pequeno às vezes explica antes do JSON; não é motivo para falhar."""
    respx.post(f"{ENDPOINT}/chat/completions").mock(
        return_value=_resposta_chat(
            'Claro! Segue:\n'
            '{"usa_ia": false, "confianca": 0.7, "justificativa": "PACS"}\n'
            'Espero ter ajudado.'
        )
    )
    async with QwenClient(ENDPOINT, MODELO) as cliente:
        resultado = await cliente.classify("texto", "prompt")
    assert resultado.usa_ia is False


@respx.mock
async def test_classify_rejeita_resposta_sem_json() -> None:
    respx.post(f"{ENDPOINT}/chat/completions").mock(return_value=_resposta_chat("não sei dizer"))
    async with QwenClient(ENDPOINT, MODELO) as cliente:
        with pytest.raises(QwenIndisponivel):
            await cliente.classify("texto", "prompt")


@respx.mock
async def test_retry_ate_conseguir() -> None:
    rota = respx.post(f"{ENDPOINT}/chat/completions")
    rota.side_effect = [
        httpx.Response(503),
        _resposta_chat('{"usa_ia": true, "confianca": 0.8, "justificativa": "ok"}'),
    ]
    async with QwenClient(ENDPOINT, MODELO, max_tentativas=3) as cliente:
        resultado = await cliente.classify("texto", "prompt")
    assert resultado.usa_ia is True
    assert rota.call_count == 2


@respx.mock
async def test_desiste_apos_max_tentativas() -> None:
    rota = respx.post(f"{ENDPOINT}/chat/completions").mock(return_value=httpx.Response(500))
    async with QwenClient(ENDPOINT, MODELO, max_tentativas=2) as cliente:
        with pytest.raises(QwenIndisponivel):
            await cliente.chat([{"role": "user", "content": "oi"}])
    assert rota.call_count == 2


# --- tool ponta a ponta -----------------------------------------------------


@respx.mock
async def test_llm_fora_do_ar_nao_derruba_a_tool(caminho_db: str) -> None:
    """Requisito: ODS fora do ar devolve erro claro, não trava o MCP."""
    respx.get(f"{ENDPOINT}/models").mock(side_effect=httpx.ConnectError("recusado"))

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
    )
    assert resposta.total == 2
    assert all(item.classificacao.origem == "indisponivel" for item in resposta.resultados)
    assert all(item.classificacao.usa_ia is None for item in resposta.resultados)


@respx.mock
async def test_usa_cache_em_vez_de_reclassificar(caminho_db: str) -> None:
    """O mesmo registro não pode custar uma chamada de LLM a cada consulta."""
    with conectar(caminho_db) as conexao:
        _inserir_dispositivo(conexao, numero="8.1", nome="DETECTOR", descricao="rede neural")
        gravar_classificacao(
            conexao,
            numero_registro="8.1",
            usa_ia=True,
            confianca=0.9,
            justificativa="cacheado",
            modelo=MODELO,
        )

    respx.get(f"{ENDPOINT}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    rota_chat = respx.post(f"{ENDPOINT}/chat/completions")

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
    )
    assert resposta.resultados[0].classificacao.origem == "cache"
    assert rota_chat.call_count == 0


@respx.mock
async def test_apenas_com_ia_filtra(caminho_db: str) -> None:
    with conectar(caminho_db) as conexao:
        _inserir_dispositivo(conexao, numero="8.1", nome="COM IA", descricao="rede neural")
        _inserir_dispositivo(conexao, numero="8.2", nome="SEM IA", descricao="pacs")
        gravar_classificacao(
            conexao,
            numero_registro="8.1",
            usa_ia=True,
            confianca=0.9,
            justificativa="ia",
            modelo=MODELO,
        )
        gravar_classificacao(
            conexao,
            numero_registro="8.2",
            usa_ia=False,
            confianca=0.9,
            justificativa="sem ia",
            modelo=MODELO,
        )

    respx.get(f"{ENDPOINT}/models").mock(return_value=httpx.Response(200, json={"data": []}))

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=True,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
    )
    assert [r.nome_produto for r in resposta.resultados] == ["COM IA"]


async def test_dias_invalido(caminho_db: str) -> None:
    resposta = await buscar_samd_recentes(
        dias=0, caminho_db=caminho_db, qwen_endpoint=ENDPOINT, qwen_model=MODELO
    )
    assert resposta.total == 0
    assert resposta.aviso is not None


@respx.mock
async def test_classificacao_sempre_marcada_como_heuristica(caminho_db: str) -> None:
    """Requisito: nunca apresentar como fato regulatório."""
    respx.get(f"{ENDPOINT}/models").mock(side_effect=httpx.ConnectError("recusado"))
    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
    )
    assert all(item.classificacao.heuristica for item in resposta.resultados)
    assert resposta.aviso is not None and "heurística" in resposta.aviso
