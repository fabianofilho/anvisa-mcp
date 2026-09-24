"""Entrypoint do servidor MCP (transporte stdio).

Registra as duas tools. Nenhuma depende da outra ter rodado antes.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from anvisa_mcp.config import carregar_config
from anvisa_mcp.mcp_server.capacidades import esconder_o_que_nao_existe
from anvisa_mcp.mcp_server.limite import LimitadorPorOrigem, origem_da_requisicao
from anvisa_mcp.mcp_server.tools.medicamentos import (
    RespostaMedicamentos,
)
from anvisa_mcp.mcp_server.tools.medicamentos import (
    consultar_status_medicamento as _consultar_status_medicamento,
)
from anvisa_mcp.mcp_server.tools.samd import (
    RespostaSaMD,
)
from anvisa_mcp.mcp_server.tools.samd import (
    buscar_samd_recentes as _buscar_samd_recentes,
)

logger = logging.getLogger(__name__)

mcp = MCPServer("anvisa-mcp", version="0.1.0")

# Tetos dos parametros das tools. O connector e publico: sem teto, uma chamada
# com limite=1000000 devolve a base inteira (9 MB) e um cliente em laco vira
# gigabytes por minuto de resposta.
LIMITE_MAXIMO = 200
DIAS_MAXIMO = 3650


@mcp.tool()
async def consultar_status_medicamento(
    nome_ou_principio_ativo: str,
    limite: Annotated[int, Field(ge=1, le=LIMITE_MAXIMO)] = 20,
) -> RespostaMedicamentos:
    """Consulta o status do registro de um medicamento na Anvisa.

    Busca por nome comercial ou princípio ativo e devolve os registros que casam,
    com situação (válido, caducado, em análise), número de registro, data de
    vencimento e empresa detentora.

    **Um princípio ativo comum tem centenas de registros**, um por detentor e
    apresentação: "dipirona" casa com 557. A resposta traz `total` (quantos casam
    na base inteira) e `retornados` (quantos vieram aqui). Quando `truncado` é
    True, não conte os resultados para dizer quantos existem, use `total`.

    Quando a base local ainda não foi sincronizada, devolve dados de exemplo com
    fonte='mock', nesse caso, não trate como informação regulatória.

    Args:
        nome_ou_principio_ativo: nome comercial ou princípio ativo, ex.: "dipirona".
        limite: quantos registros trazer. Aumente para ver além dos primeiros.
    """
    config = carregar_config()
    return await _consultar_status_medicamento(
        nome_ou_principio_ativo, caminho_db=str(config.duckdb_path), limite=limite
    )


@mcp.tool()
async def buscar_samd_recentes(
    dias: Annotated[int, Field(ge=1, le=DIAS_MAXIMO)] = 90,
    apenas_com_ia: bool = True,
    apenas_software: bool = True,
    limite: Annotated[int, Field(ge=1, le=LIMITE_MAXIMO)] = 50,
) -> RespostaSaMD:
    """Lista dispositivos médicos Classe III/IV registrados recentemente na Anvisa.

    Para cada registro, classifica se o produto usa IA ou aprendizado de máquina.
    A Anvisa não publica esse campo: a classificação é heurística, feita por um LLM
    local lendo o texto do registro, e cada item traz confiança e justificativa.
    Não apresente o veredito como fato regulatório.

    Args:
        dias: tamanho da janela, em dias, a contar de hoje.
        apenas_com_ia: quando True, devolve só os classificados como usando IA.
        limite: quantos registros analisar nesta chamada, dos mais recentes para trás.
            Com `truncado=true` na resposta, sobraram registros no período que nem
            foram olhados: a contagem da lista não serve para dizer quantos existem.
        apenas_software: quando True, analisa só registros cujo texto sugere software.
            SaMD é raro no registro (13 de 1.832 registros Classe III/IV do último ano
            mencionam software), então sem esse filtro a busca gasta as chamadas de LLM
            em cânulas e parafusos. Desligue para varrer tudo, ao custo de ser lento.
    """
    config = carregar_config()
    return await _buscar_samd_recentes(
        dias=dias,
        apenas_com_ia=apenas_com_ia,
        apenas_software=apenas_software,
        limite=limite,
        caminho_db=str(config.duckdb_path),
        qwen_endpoint=config.qwen_endpoint,
        qwen_model=config.qwen_model,
        timeout_segundos=config.qwen_timeout_segundos,
        max_tentativas=config.qwen_max_tentativas,
        # No modo connector o servidor nao chama o LLM: serve o cache.
        permitir_llm=not config.modo_connector,
    )


def _seguranca_de_transporte(config: Any) -> Any:
    """Regras de Host/Origin quando o servidor atende por um nome publico.

    O SDK so liga a protecao contra DNS rebinding sozinho quando o bind e
    loopback, e ai aceita apenas Host de loopback. Atras de um tunel ou proxy o
    Host que chega e o nome publico, e a requisicao legitima levaria 421. Em vez
    de desligar a checagem, declaramos os nomes por onde o servidor responde.

    Devolve None quando nao ha nome publico configurado, deixando o padrao do
    SDK valer.
    """
    if not config.http_hosts_publicos:
        return None

    from mcp.server.transport_security import TransportSecuritySettings

    hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origens: list[str] = ["http://127.0.0.1:*", "http://localhost:*"]
    for nome in config.http_hosts_publicos:
        # Com e sem porta: atras de TLS o Host costuma vir sem o ":443".
        hosts += [nome, f"{nome}:*"]
        origens += [f"https://{nome}", f"https://{nome}:*"]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origens)


def _com_limite(app: Any, limite_por_minuto: int, limite_global: int) -> Any:
    """Embrulha o app ASGI com o teto de requisicoes por origem.

    O `run()` do SDK nao aceita middleware, entao o app e construido por
    `streamable_http_app()`, embrulhado aqui e servido por uvicorn.
    """
    limitador = LimitadorPorOrigem(limite_por_minuto, limite_global)

    async def middleware(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        origem = origem_da_requisicao(scope)
        if not limitador.permitir(origem):
            logger.warning("limite %s excedido (origem %s)", limitador.motivo_ultima_recusa, origem)
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"60"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"erro":"limite de requisicoes excedido; tente em 1 minuto"}',
                }
            )
            return
        await app(scope, receive, send)

    return middleware


def main() -> None:
    """Sobe o servidor MCP. Stdio por padrao; HTTP no modo connector."""
    config = carregar_config()
    # Este servidor so tem tools. Anunciar prompts e resources faria quem mapeia
    # o servidor gastar chamadas para descobrir lista vazia.
    esconder_o_que_nao_existe(mcp)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if config.transporte == "stdio":
        logger.info("anvisa-mcp subindo em stdio (LLM em %s)", config.qwen_endpoint)
        mcp.run(transport="stdio")
        return

    # Modo connector: somente leitura, sem LLM, e o sync roda fora deste
    # processo publicando a base por troca atomica.
    import uvicorn

    app = _com_limite(
        mcp.streamable_http_app(
            streamable_http_path=config.http_path,
            stateless_http=config.http_stateless,
            host=config.http_host,
            transport_security=_seguranca_de_transporte(config),
        ),
        config.http_limite_por_minuto,
        config.http_limite_global_por_minuto,
    )
    logger.info(
        "anvisa-mcp em http://%s:%d%s (stateless=%s, sem LLM, limite %d/min por origem "
        "e %d/min global, base=%s)",
        config.http_host,
        config.http_porta,
        config.http_path,
        config.http_stateless,
        config.http_limite_por_minuto,
        config.http_limite_global_por_minuto,
        config.duckdb_path,
    )
    uvicorn.run(app, host=config.http_host, port=config.http_porta, log_level="warning")
