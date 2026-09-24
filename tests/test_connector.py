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
    assert resposta.retornados == 1
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
    assert resposta.retornados == 0, "não classificado não pode entrar como 'usa IA'"
    assert resposta.total == 1, "o registro existe no período e precisa aparecer no universo"
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


def test_sem_host_publico_mantem_o_padrao_do_sdk() -> None:
    """Sem nome público declarado, quem decide é o SDK: só loopback."""
    from anvisa_mcp.config import Config
    from anvisa_mcp.mcp_server.server import _seguranca_de_transporte

    assert _seguranca_de_transporte(Config(http_hosts_publicos=[])) is None


def test_host_publico_entra_sem_derrubar_o_loopback() -> None:
    """Declarar o nome do túnel não pode cortar o acesso local, que é como se testa."""
    from anvisa_mcp.config import Config
    from anvisa_mcp.mcp_server.server import _seguranca_de_transporte

    regras = _seguranca_de_transporte(Config(http_hosts_publicos=["mcp.exemplo.ts.net"]))
    assert regras is not None
    assert regras.enable_dns_rebinding_protection is True
    assert "mcp.exemplo.ts.net" in regras.allowed_hosts
    assert "127.0.0.1:*" in regras.allowed_hosts
    assert "https://mcp.exemplo.ts.net" in regras.allowed_origins


def test_host_de_fora_da_lista_continua_recusado() -> None:
    """A proteção contra DNS rebinding continua valendo para quem não foi declarado."""
    from mcp.server.transport_security import TransportSecurityMiddleware

    from anvisa_mcp.config import Config
    from anvisa_mcp.mcp_server.server import _seguranca_de_transporte

    guarda = TransportSecurityMiddleware(
        _seguranca_de_transporte(Config(http_hosts_publicos=["mcp.exemplo.ts.net"]))
    )
    assert guarda._validate_host("mcp.exemplo.ts.net") is True
    assert guarda._validate_host("mcp.exemplo.ts.net:443") is True
    assert guarda._validate_host("site-do-atacante.exemplo") is False
    assert guarda._validate_host(None) is False


def test_nao_anuncia_prompts_nem_resources_sem_ter_nenhum() -> None:
    """Handshake prometendo lista vazia custa chamada e sugere recurso inexistente."""
    from mcp.server.mcpserver import MCPServer

    from anvisa_mcp.mcp_server.capacidades import esconder_o_que_nao_existe

    servidor = MCPServer("teste", version="0.1")

    @servidor.tool()
    async def exemplo(x: str) -> str:
        """Tool qualquer."""
        return x

    esconder_o_que_nao_existe(servidor)
    capacidades = servidor._lowlevel_server.get_capabilities()

    assert capacidades.prompts is None
    assert capacidades.resources is None
    assert capacidades.tools is not None, "tools continua anunciado"


def test_recurso_cadastrado_continua_anunciado() -> None:
    """A supressão não pode esconder o que de fato existe."""
    from mcp.server.mcpserver import MCPServer

    from anvisa_mcp.mcp_server.capacidades import esconder_o_que_nao_existe

    servidor = MCPServer("teste", version="0.1")

    @servidor.resource("config://exemplo")
    def recurso() -> str:
        """Um recurso de verdade."""
        return "conteudo"

    esconder_o_que_nao_existe(servidor)
    capacidades = servidor._lowlevel_server.get_capabilities()

    assert capacidades.resources is not None, "existe recurso, tem que ser anunciado"
    assert capacidades.prompts is None


def test_sdk_diferente_nao_derruba_o_servidor() -> None:
    """Isto mexe em estrutura interna do SDK: mudança lá não pode virar exceção aqui."""
    from anvisa_mcp.mcp_server.capacidades import esconder_o_que_nao_existe

    class Estranho:
        pass

    assert esconder_o_que_nao_existe(Estranho()) == ()
