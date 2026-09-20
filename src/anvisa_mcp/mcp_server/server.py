"""Entrypoint do servidor MCP (transporte stdio).

Registra as duas tools. Nenhuma depende da outra ter rodado antes.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from anvisa_mcp.config import carregar_config
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


@mcp.tool()
async def consultar_status_medicamento(nome_ou_principio_ativo: str) -> RespostaMedicamentos:
    """Consulta o status do registro de um medicamento na Anvisa.

    Busca por nome comercial ou princípio ativo e devolve todos os registros que
    casam, com situação (válido, caducado, em análise), número de registro, data e
    empresa detentora. Quando a base local ainda não foi sincronizada, devolve
    dados de exemplo com fonte='mock' — nesse caso, não trate como informação
    regulatória.

    Args:
        nome_ou_principio_ativo: nome comercial ou princípio ativo, ex.: "dipirona".
    """
    config = carregar_config()
    return await _consultar_status_medicamento(
        nome_ou_principio_ativo, caminho_db=str(config.duckdb_path)
    )


@mcp.tool()
async def buscar_samd_recentes(
    dias: int = 90, apenas_com_ia: bool = True, apenas_software: bool = True
) -> RespostaSaMD:
    """Lista dispositivos médicos Classe III/IV registrados recentemente na Anvisa.

    Para cada registro, classifica se o produto usa IA ou aprendizado de máquina.
    A Anvisa não publica esse campo: a classificação é heurística, feita por um LLM
    local lendo o texto do registro, e cada item traz confiança e justificativa.
    Não apresente o veredito como fato regulatório.

    Args:
        dias: tamanho da janela, em dias, a contar de hoje.
        apenas_com_ia: quando True, devolve só os classificados como usando IA.
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
        caminho_db=str(config.duckdb_path),
        qwen_endpoint=config.qwen_endpoint,
        qwen_model=config.qwen_model,
        timeout_segundos=config.qwen_timeout_segundos,
        max_tentativas=config.qwen_max_tentativas,
    )


def main() -> None:
    """Sobe o servidor MCP no stdio. Logs vão para stderr: stdout é o transporte."""
    config = carregar_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("anvisa-mcp subindo (LLM em %s)", config.qwen_endpoint)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
