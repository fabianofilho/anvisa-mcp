"""Classificador Jev (TypeSafe) atrás de flag: mapeamento, fallback e cache.

Nenhum teste toca a rede nem lê a chave real: o respx intercepta o httpx e a
chave é injetada como texto.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from anvisa_mcp.config import ChaveJevAusente, Config
from anvisa_mcp.llm.jev_client import (
    ClassificadorComFallback,
    JevClient,
    JevIndisponivel,
    ParametrosJev,
    montar_state,
    resultado_de_probabilidade,
)
from anvisa_mcp.llm.qwen_client import QwenClient, QwenIndisponivel
from anvisa_mcp.mcp_server.tools.samd import buscar_samd_recentes
from anvisa_mcp.store.db import conectar
from anvisa_mcp.store.queries import classificacao_em_cache

QWEN = "http://llm-de-teste/v1"
QWEN_MODELO = "qwen-de-teste"
JEV = "https://jev-de-teste/v1/systemone"
JEV_MODELOS = "https://jev-de-teste/v1/models"
PARAMETROS = ParametrosJev(chave="chave-de-teste", endpoint=JEV, modelo="jev-1.13.0")


def _resposta_jev(p_usa_ia: float, p_descreve: float = 0.05) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-1.13.0",
            "answers": {
                "usa_ia": {"type": "noul", "noul": p_usa_ia},
                "texto_descreve_funcionamento": {"type": "noul", "noul": p_descreve},
            },
            "usage": {"input_tokens": 600, "output_tokens": 40},
        },
    )


def _resposta_chat(conteudo: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": conteudo}}]})


def _inserir_dispositivo(caminho_db: str, numero: str, nome: str, descricao: str) -> None:
    with conectar(caminho_db) as conexao:
        conexao.execute(
            """
            INSERT INTO dispositivos_medicos
                (numero_registro, nome_produto, empresa_detentora, classe_risco,
                 situacao, data_registro, descricao)
            VALUES (?, ?, 'EMPRESA', 'III', 'válido', current_date, ?)
            """,
            [numero, nome, descricao],
        )


# --- mapeamento puro ---------------------------------------------------------


def test_montar_state_separa_os_tres_campos_da_anvisa() -> None:
    state = montar_state(
        "CAD4TB", "Nome técnico: Software. Nome comercial: CAD4TB. Fabricante: DELFT AI B.V."
    )
    assert state == {
        "nome_tecnico": "Software",
        "nome_comercial": "CAD4TB",
        "fabricante": "DELFT AI B.V",
    }


def test_montar_state_sem_padrao_nao_perde_texto() -> None:
    state = montar_state("X", "texto livre qualquer")
    assert state["nome_comercial"] == "texto livre qualquer"
    assert state["nome_tecnico"] == "" and state["fabricante"] == ""


@pytest.mark.parametrize(
    ("p", "usa_ia", "confianca"),
    [(0.88, True, 0.76), (0.64, True, 0.28), (0.5, True, 0.0), (0.02, False, 0.96)],
)
def test_probabilidade_vira_veredito_e_confianca_2p_menos_1(
    p: float, usa_ia: bool, confianca: float
) -> None:
    resultado = resultado_de_probabilidade(p, 0.1, "jev-1.13.0")
    assert resultado.usa_ia is usa_ia
    assert resultado.confianca == pytest.approx(confianca)
    assert resultado.termos_citados == []
    assert resultado.modelo == "jev-1.13.0"
    assert "Sem trechos citados" in resultado.justificativa


# --- cliente -------------------------------------------------------------------


@respx.mock
async def test_jev_classificar_envia_state_nomeado_com_a_chave() -> None:
    rota = respx.post(JEV).mock(return_value=_resposta_jev(0.88))
    async with JevClient(PARAMETROS) as cliente:
        resultado = await cliente.classificar(
            nome="CAD4TB",
            descricao="Nome técnico: Software. Nome comercial: CAD4TB. Fabricante: DELFT AI",
        )
    pedido = rota.calls.last.request
    assert pedido.headers["Authorization"] == "Bearer chave-de-teste"
    corpo = pedido.read()
    assert b'"nome_tecnico"' in corpo and b'"fabricante"' in corpo and b'"model"' in corpo
    assert resultado.usa_ia is True and resultado.modelo == "jev-1.13.0"


@respx.mock
async def test_jev_401_e_erro_de_rede_viram_indisponivel_que_a_tool_ja_captura() -> None:
    respx.post(JEV).mock(return_value=httpx.Response(401, text="bad key"))
    async with JevClient(PARAMETROS) as cliente:
        with pytest.raises(JevIndisponivel, match="HTTP 401"):
            await cliente.classificar(nome="X", descricao="")
    assert issubclass(JevIndisponivel, QwenIndisponivel)

    respx.post(JEV).mock(side_effect=httpx.ConnectError("recusado"))
    async with JevClient(PARAMETROS) as cliente:
        with pytest.raises(JevIndisponivel):
            await cliente.classificar(nome="X", descricao="")


@respx.mock
async def test_fallback_usa_o_qwen_e_registra_quem_respondeu() -> None:
    respx.get(JEV_MODELOS).mock(return_value=httpx.Response(200, json={"models": []}))
    respx.post(JEV).mock(return_value=httpx.Response(500))
    respx.get(f"{QWEN}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(f"{QWEN}/chat/completions").mock(
        return_value=_resposta_chat(
            '{"usa_ia": false, "confianca": 0.2, "justificativa": "texto insuficiente", '
            '"termos_citados": []}'
        )
    )
    async with (
        QwenClient(QWEN, QWEN_MODELO) as qwen,
        JevClient(PARAMETROS) as jev,
    ):
        classificador = ClassificadorComFallback(jev, qwen)
        assert await classificador.esta_vivo() is True
        resultado = await classificador.classificar(nome="X", descricao="", prompt_qwen="p")
    assert resultado.usa_ia is False
    assert resultado.modelo == QWEN_MODELO


@respx.mock
async def test_sem_qwen_vivo_a_falha_do_jev_sobe() -> None:
    respx.get(JEV_MODELOS).mock(return_value=httpx.Response(200, json={"models": []}))
    respx.post(JEV).mock(return_value=httpx.Response(529))
    respx.get(f"{QWEN}/models").mock(side_effect=httpx.ConnectError("fora"))
    async with (
        QwenClient(QWEN, QWEN_MODELO) as qwen,
        JevClient(PARAMETROS) as jev,
    ):
        classificador = ClassificadorComFallback(jev, qwen)
        assert await classificador.esta_vivo() is True  # o Jev respondeu ao /models
        with pytest.raises(JevIndisponivel):
            await classificador.classificar(nome="X", descricao="", prompt_qwen="p")


# --- tool com a flag -------------------------------------------------------------


@respx.mock
async def test_flag_desligada_nao_chama_o_jev(caminho_db: str) -> None:
    """A garantia de 'nada mudou': sem ``jev``, nenhum byte vai para a nuvem."""
    _inserir_dispositivo(
        caminho_db, "1", "DETECTOR", "Nome técnico: Software. Nome comercial: DETECTOR"
    )
    rota_jev = respx.post(JEV)
    respx.get(f"{QWEN}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(f"{QWEN}/chat/completions").mock(
        return_value=_resposta_chat(
            '{"usa_ia": true, "confianca": 0.9, "justificativa": "ok", '
            '"termos_citados": ["DETECTOR"]}'
        )
    )
    resposta = await buscar_samd_recentes(
        dias=30,
        apenas_com_ia=False,
        apenas_software=False,
        caminho_db=caminho_db,
        qwen_endpoint=QWEN,
        qwen_model=QWEN_MODELO,
    )
    assert resposta.resultados[0].classificacao.origem == "llm"
    assert rota_jev.call_count == 0


@respx.mock
async def test_flag_ligada_grava_modelo_jev_e_evidencia_confere(caminho_db: str) -> None:
    _inserir_dispositivo(
        caminho_db,
        "2",
        "CAD4TB",
        "Nome técnico: Software. Nome comercial: CAD4TB. Fabricante: DELFT AI",
    )
    respx.get(JEV_MODELOS).mock(return_value=httpx.Response(200, json={"models": []}))
    rota_jev = respx.post(JEV).mock(return_value=_resposta_jev(0.88))
    respx.get(f"{QWEN}/models").mock(side_effect=httpx.ConnectError("qwen desligado"))
    rota_qwen = respx.post(f"{QWEN}/chat/completions")

    resposta = await buscar_samd_recentes(
        dias=30,
        apenas_com_ia=False,
        apenas_software=False,
        caminho_db=caminho_db,
        gravar_cache=True,
        qwen_endpoint=QWEN,
        qwen_model=QWEN_MODELO,
        jev=PARAMETROS,
    )
    item = resposta.resultados[0].classificacao
    assert item.origem == "llm" and item.usa_ia is True
    assert item.confianca == pytest.approx(0.76)
    assert item.evidencia_confere is True
    assert rota_jev.call_count == 1 and rota_qwen.call_count == 0

    with conectar(caminho_db, somente_leitura=True) as conexao:
        cacheado = classificacao_em_cache(conexao, "2")
    assert cacheado is not None
    assert cacheado["modelo"] == "jev-1.13.0"
    # True, não None: None faria o --reclassificar noturno refazer o veredito toda noite.
    assert cacheado["evidencia_confere"] is True


@respx.mock
async def test_sim_fraco_do_jev_sai_com_confianca_baixa_visivel(caminho_db: str) -> None:
    """p=0,64 vira usa_ia=True com confiança 0,28. A tool devolve o item: o filtro de
    apenas_com_ia só descarta "não". A dúvida fica visível na confiança, não escondida."""
    _inserir_dispositivo(caminho_db, "3", "AI2 ROBO", "Nome técnico: Robô. Nome comercial: AI2")
    respx.get(JEV_MODELOS).mock(return_value=httpx.Response(200, json={"models": []}))
    respx.post(JEV).mock(return_value=_resposta_jev(0.64))
    respx.get(f"{QWEN}/models").mock(side_effect=httpx.ConnectError("qwen desligado"))

    resposta = await buscar_samd_recentes(
        dias=30,
        apenas_com_ia=True,
        apenas_software=False,
        caminho_db=caminho_db,
        qwen_endpoint=QWEN,
        qwen_model=QWEN_MODELO,
        jev=PARAMETROS,
    )
    assert len(resposta.resultados) == 1
    item = resposta.resultados[0].classificacao
    assert item.usa_ia is True
    assert item.confianca == pytest.approx(0.28)
    assert resposta.indeterminados == 0


# --- config ----------------------------------------------------------------------


def test_config_padrao_e_qwen_sem_parametros_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLASSIFICADOR_SAMD", raising=False)
    config = Config(_env_file=None)  # type: ignore[call-arg]
    assert config.classificador_samd == "qwen"
    assert config.parametros_jev() is None


def test_config_jev_sem_chave_falha_alto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = Config(  # type: ignore[call-arg]
        _env_file=None, classificador_samd="jev", jev_arquivo_chave=tmp_path / "nao-existe"
    )
    with pytest.raises(ChaveJevAusente):
        config.parametros_jev()


def test_config_jev_le_chave_do_arquivo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    arquivo = tmp_path / "env"
    arquivo.write_text("# comentário\nTYPESAFE_API_KEY=abc\n", encoding="utf-8")
    config = Config(  # type: ignore[call-arg]
        _env_file=None, classificador_samd="jev", jev_arquivo_chave=arquivo, jev_model="jev-1.13.0"
    )
    parametros = config.parametros_jev()
    assert parametros is not None
    assert parametros.chave == "abc" and parametros.modelo == "jev-1.13.0"
