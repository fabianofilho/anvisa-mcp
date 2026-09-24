"""Classificação de uso de IA em dispositivos médicos, via LLM local.

A Anvisa não publica um campo estruturado "usa IA": esta camada preenche a lacuna
lendo o texto livre do registro. O resultado é heurístico, e quem chama precisa
tratá-lo como tal (ver ``confianca`` e ``justificativa``).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from importlib.resources import files

from jinja2 import Environment, Template, select_autoescape

from anvisa_mcp.llm.qwen_client import ClassificationResult, QwenClient

logger = logging.getLogger(__name__)

NOME_TEMPLATE = "classificar_samd_ia.jinja2"


def texto_do_prompt(nome: str = NOME_TEMPLATE) -> str:
    """Conteúdo de um template em ``anvisa_mcp/prompts``.

    Lido como recurso do pacote, não por caminho relativo à raiz do repositório:
    assim o prompt vai junto no wheel e funciona fora do checkout.
    """
    return files("anvisa_mcp").joinpath("prompts", nome).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _template() -> Template:
    ambiente = Environment(autoescape=select_autoescape(default=False, default_for_string=False))
    return ambiente.from_string(texto_do_prompt())


def renderizar_prompt(**variaveis: object) -> str:
    """Renderiza o template de classificação."""
    return _template().render(**variaveis)


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
