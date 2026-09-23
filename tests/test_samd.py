"""Tool de SaMD: classificação, cache e degradação quando o LLM cai."""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import httpx
import pytest
import respx

from anvisa_mcp.llm.qwen_client import ClassificationResult, QwenClient, QwenIndisponivel
from anvisa_mcp.mcp_server.tools.samd import buscar_samd_recentes
from anvisa_mcp.store.db import aplicar_schema, conectar
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
            "Claro! Segue:\n"
            '{"usa_ia": false, "confianca": 0.7, "justificativa": "PACS"}\n'
            "Espero ter ajudado."
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
        apenas_software=False,
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
        apenas_software=False,
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


def test_ordem_estavel_com_datas_empatadas(db: duckdb.DuckDBPyConnection) -> None:
    """Sem desempate, o LIMIT devolve um conjunto diferente a cada chamada."""
    for numero in ("300", "100", "200"):
        _inserir_dispositivo(db, numero=numero, nome=f"DISP {numero}", dias_atras=5)

    primeira = [d["numero_registro"] for d in dispositivos_no_periodo(db, dias=90, limite=2)]
    segunda = [d["numero_registro"] for d in dispositivos_no_periodo(db, dias=90, limite=2)]
    assert primeira == segunda == ["100", "200"]


# --- pré-filtro de software ------------------------------------------------


def test_filtro_software_descarta_hardware(db: duckdb.DuckDBPyConnection) -> None:
    """SaMD é raro: sem filtro, o LLM é gasto em cânulas e parafusos."""
    _inserir_dispositivo(
        db, numero="1", nome="PARAFUSO PEDICULAR", descricao="Nome técnico: parafuso ósseo"
    )
    _inserir_dispositivo(
        db, numero="2", nome="CAD4TB", descricao="Software para detecção de tuberculose"
    )

    com_filtro = dispositivos_no_periodo(db, dias=90, apenas_software=True)
    sem_filtro = dispositivos_no_periodo(db, dias=90, apenas_software=False)

    assert [d["nome_produto"] for d in com_filtro] == ["CAD4TB"]
    assert len(sem_filtro) == 2


def test_filtro_software_olha_tambem_o_nome(db: duckdb.DuckDBPyConnection) -> None:
    """O nome comercial às vezes é o único lugar onde 'software' aparece."""
    _inserir_dispositivo(db, numero="1", nome="Software de contorno em radioterapia", descricao="")
    assert len(dispositivos_no_periodo(db, dias=90, apenas_software=True)) == 1


def test_filtro_software_nao_pega_sistema_de_joelho(db: duckdb.DuckDBPyConnection) -> None:
    """'Sistema de' pegaria hardware ortopédico: por isso não está na lista."""
    _inserir_dispositivo(
        db, numero="1", nome="Sistema de Joelho MOBIO PS", descricao="Nome técnico: prótese"
    )
    assert dispositivos_no_periodo(db, dias=90, apenas_software=True) == []


@respx.mock
async def test_aviso_conta_que_houve_filtro(caminho_db: str) -> None:
    """Quem lê precisa saber que a varredura não foi completa."""
    with conectar(caminho_db) as conexao:
        _inserir_dispositivo(conexao, numero="8.1", nome="CAD4TB", descricao="Software de detecção")
        gravar_classificacao(
            conexao,
            numero_registro="8.1",
            usa_ia=True,
            confianca=0.9,
            justificativa="cacheado",
            modelo=MODELO,
        )

    respx.get(f"{ENDPOINT}/models").mock(side_effect=httpx.ConnectError("recusado"))
    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        apenas_software=True,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
    )
    assert resposta.fonte == "duckdb"
    assert resposta.aviso is not None
    assert "apenas_software=False" in resposta.aviso


@respx.mock
async def test_negativo_de_baixa_confianca_vira_indeterminado(caminho_db: str) -> None:
    """'Não usa IA' com confiança 0,2 quer dizer 'não dá para saber', não some calado."""
    with conectar(caminho_db) as conexao:
        _inserir_dispositivo(conexao, numero="8.1", nome="BoneCT", descricao="Software")
        _inserir_dispositivo(conexao, numero="8.2", nome="PARAFUSO", descricao="Software")
        gravar_classificacao(
            conexao,
            numero_registro="8.1",
            usa_ia=False,
            confianca=0.2,
            justificativa="só nome e fabricante",
            modelo=MODELO,
        )
        gravar_classificacao(
            conexao,
            numero_registro="8.2",
            usa_ia=False,
            confianca=0.95,
            justificativa="parafuso físico",
            modelo=MODELO,
        )

    respx.get(f"{ENDPOINT}/models").mock(side_effect=httpx.ConnectError("recusado"))
    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=True,
        apenas_software=True,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
    )
    assert resposta.retornados == 0
    assert resposta.indeterminados == 1
    assert resposta.aviso is not None and "não significa que não usem IA" in resposta.aviso


@pytest.mark.asyncio
async def test_total_e_o_universo_do_periodo_nao_o_tamanho_da_lista(caminho_db: str) -> None:
    """Dizer 'total: 50' com 1832 no período vira contagem errada de SaMD registrados."""
    with conectar(caminho_db) as conexao:
        aplicar_schema(conexao)
        conexao.executemany(
            "INSERT INTO dispositivos_medicos (numero_registro, nome_produto, classe_risco, "
            "data_registro, descricao) VALUES (?, ?, 'III', current_date, 'cânula comum')",
            [[str(i), f"DISPOSITIVO {i}"] for i in range(30)],
        )

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        apenas_software=False,
        caminho_db=caminho_db,
        limite=10,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
        permitir_llm=False,
    )

    assert resposta.total == 30, "o universo do período, não a página"
    assert resposta.analisados == 10
    assert resposta.truncado is True
    assert resposta.aviso is not None and "30" in resposta.aviso


def test_validade_sai_do_campo_de_situacao() -> None:
    """O campo mistura 'VIGENTE' com data; quem lê via '14/09/2036' e adivinhava."""
    from anvisa_mcp.mcp_server.tools.samd import _validade

    assert _validade("14/09/2036") == date(2036, 9, 14)
    assert _validade("VIGENTE") is None
    assert _validade(None) is None
    assert _validade("") is None


def test_evidencia_rejeita_o_que_nao_esta_no_registro() -> None:
    """Caso real: um teste rápido descrito como 'mangueira biológica'."""
    from anvisa_mcp.llm.evidencia import verificar

    registro = "TR COVID-19 AG. TESTE RAPIDO ANTIGENO. FIOCRUZ"
    assert verificar(["mangueira biológica"], registro).confere is False
    assert verificar(["TESTE RAPIDO ANTIGENO"], registro).confere is True


def test_evidencia_aceita_parafrase_com_acento_e_caixa() -> None:
    """O alvo é invenção, não estilo: reprovar paráfrase honesta daria ruído."""
    from anvisa_mcp.llm.evidencia import verificar

    assert verificar(["teste rápido de antígeno"], "TESTE RAPIDO ANTIGENO FIOCRUZ").confere is True


def test_evidencia_vazia_e_honesta() -> None:
    """Não afirmar nada não é erro: o prompt manda baixar a confiança nesse caso."""
    from anvisa_mcp.llm.evidencia import verificar

    assert verificar([], "qualquer texto").confere is True


@respx.mock
async def test_classificacao_marca_quando_o_modelo_inventa(caminho_db: str) -> None:
    """O veredito pode estar certo e a justificativa não se sustentar no texto."""
    with conectar(caminho_db) as conexao:
        _inserir_dispositivo(conexao, numero="9.1", nome="TR COVID-19 AG", descricao="FIOCRUZ")

    respx.get(f"{ENDPOINT}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post(f"{ENDPOINT}/chat/completions").mock(
        return_value=_resposta_chat(
            '{"usa_ia": false, "confianca": 0.6, "justificativa": "mangueira biológica '
            'sem software", "termos_citados": ["mangueira biológica"]}'
        )
    )

    resposta = await buscar_samd_recentes(
        dias=90,
        apenas_com_ia=False,
        apenas_software=False,
        caminho_db=caminho_db,
        qwen_endpoint=ENDPOINT,
        qwen_model=MODELO,
    )

    classificacao = resposta.resultados[0].classificacao
    assert classificacao.usa_ia is False, "o veredito segue valendo"
    assert classificacao.evidencia_confere is False, "mas a evidência não está no registro"
