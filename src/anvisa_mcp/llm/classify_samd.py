"""Classificação de uso de IA em dispositivos médicos, via LLM local.

A Anvisa não publica um campo estruturado "usa IA": esta camada preenche a lacuna
lendo o texto livre do registro. O resultado é heurístico, e quem chama precisa
tratá-lo como tal (ver ``confianca`` e ``justificativa``).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from anvisa_mcp.llm.qwen_client import ClassificationResult, QwenClient

logger = logging.getLogger(__name__)

NOME_TEMPLATE = "classificar_samd_ia.jinja2"


def diretorio_prompts() -> Path:
    """Diretório ``prompts/`` na raiz do repositório."""
    return Path(__file__).resolve().parents[3] / "prompts"


@lru_cache(maxsize=1)
def _ambiente_jinja() -> Environment:
    return Environment(
        loader=FileSystemLoader(diretorio_prompts()),
        autoescape=select_autoescape(default=False, default_for_string=False),
    )


def renderizar_prompt(**variaveis: object) -> str:
    """Renderiza o template de classificação."""
    return _ambiente_jinja().get_template(NOME_TEMPLATE).render(**variaveis)


async def classificar_dispositivo(
    cliente: QwenClient,
    *,
    nome: str,
    descricao: str,
) -> ClassificationResult:
    """Classifica um dispositivo. Levanta ``QwenIndisponivel`` se o LLM falhar."""
    texto = f"Nome do produto: {nome}\n\nDocumentação:\n{descricao}".strip()
    resultado = await cliente.classify(texto, renderizar_prompt())
    logger.debug(
        "classificado %r: usa_ia=%s conf=%.2f", nome, resultado.usa_ia, resultado.confianca
    )
    return resultado
