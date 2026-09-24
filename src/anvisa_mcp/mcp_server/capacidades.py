"""Não anunciar no handshake o que o servidor não tem.

O SDK registra os handlers de ``prompts`` e ``resources`` sempre, mesmo sem
nenhum cadastrado, e as capabilities derivam dos handlers registrados. O
resultado é um handshake que promete duas coisas e entrega listas vazias, o que
custa chamadas a quem está mapeando o servidor e sugere recurso que não existe.

Isto mexe em ``_request_handlers``, que é interno ao SDK. A função é defensiva e
não levanta se a estrutura mudar, e há teste cobrindo os dois lados: com os
gerentes vazios as capabilities saem, e com um recurso cadastrado elas ficam.
Se um upgrade do SDK mudar a estrutura, o teste avisa em vez de o handshake
mudar em silêncio.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Removidos juntos: anunciar 'prompts' sem poder buscar um prompt seria pior que
# não anunciar.
_METODOS_DE_PROMPT = ("prompts/list", "prompts/get")
_METODOS_DE_RECURSO = ("resources/list", "resources/read", "resources/templates/list")


def _vazio(gerente: Any, *atributos: str) -> bool:
    """True quando o gerente não tem nada cadastrado.

    Na dúvida devolve False: anunciar capability a mais é menos grave que
    esconder uma que existe.
    """
    for atributo in atributos:
        conteudo = getattr(gerente, atributo, None)
        if conteudo is not None:
            return not conteudo
    return False


def esconder_o_que_nao_existe(mcp: Any) -> tuple[str, ...]:
    """Remove os handlers de prompts e resources quando não há nenhum.

    Devolve os métodos removidos, para log e para teste.
    """
    servidor = getattr(mcp, "_lowlevel_server", None)
    handlers = getattr(servidor, "_request_handlers", None)
    if not isinstance(handlers, dict):
        logger.debug("SDK sem _request_handlers acessível; capabilities ficam como vêm")
        return ()

    remover: list[str] = []
    if _vazio(getattr(mcp, "_prompt_manager", None), "_prompts", "prompts"):
        remover += list(_METODOS_DE_PROMPT)
    if _vazio(getattr(mcp, "_resource_manager", None), "_resources", "resources"):
        remover += list(_METODOS_DE_RECURSO)

    removidos = tuple(metodo for metodo in remover if handlers.pop(metodo, None) is not None)
    if removidos:
        logger.debug("capabilities não anunciadas: %s", ", ".join(removidos))
    return removidos
