"""Limitador de taxa para o modo connector.

Um connector aberto pode ser chamado por qualquer usuário do Claude. Sem teto,
uma única sessão mal comportada consome a máquina que serve todo mundo.

**Por que são dois limites, e não um por IP.** Quando o Claude.ai chama um
connector remoto, as requisições chegam dos IPs da Anthropic, não do usuário
final; a própria documentação manda liberar as faixas deles no firewall. Um
limite por IP colocaria todos os usuários do Claude no mesmo balde: ou é
restritivo e derruba todo mundo junto, ou é frouxo e não protege nada.

Então:

- o **limite global** é o que de fato protege a máquina, e é o teto que importa
  para tráfego vindo do Claude;
- o **limite por origem** continua útil contra quem chama o servidor direto,
  fora do Claude, e fica alto o suficiente para não atrapalhar o uso normal.

Nenhum dos dois é defesa contra ataque distribuído. São para o caso comum de
cliente em laço.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

JANELA_SEGUNDOS = 60.0
# Acima disso, para de guardar IP novo: memória não pode crescer sem teto.
MAX_ORIGENS = 10_000


class LimitadorPorOrigem:
    """Janela deslizante de um minuto, com teto global e teto por origem."""

    def __init__(self, limite_por_minuto: int, limite_global_por_minuto: int = 0) -> None:
        self._limite = limite_por_minuto
        self._limite_global = limite_global_por_minuto
        self._janelas: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()

    @property
    def motivo_ultima_recusa(self) -> str:
        """'global', 'origem' ou 'tabela', para o log dizer qual teto foi batido."""
        return self._motivo

    _motivo = "origem"

    def permitir(self, origem: str) -> bool:
        """True se a requisição cabe nos dois limites. Registra quando cabe."""
        agora = time.monotonic()

        if self._limite_global:
            while self._global and agora - self._global[0] > JANELA_SEGUNDOS:
                self._global.popleft()
            if len(self._global) >= self._limite_global:
                self._motivo = "global"
                return False

        janela = self._janelas.get(origem)
        if janela is None:
            if len(self._janelas) >= MAX_ORIGENS:
                self._podar(agora)
            if len(self._janelas) >= MAX_ORIGENS:
                # Tabela cheia só de origens ativas: recusa. Deixar passar
                # daria a quem gira origens um caminho sem limite nenhum.
                self._motivo = "tabela"
                return False
            janela = self._janelas[origem] = deque()

        while janela and agora - janela[0] > JANELA_SEGUNDOS:
            janela.popleft()

        if len(janela) >= self._limite:
            self._motivo = "origem"
            return False

        janela.append(agora)
        if self._limite_global:
            self._global.append(agora)
        return True

    def _podar(self, agora: float) -> None:
        """Remove origens sem chamada na janela atual."""
        vazias = [
            origem
            for origem, janela in self._janelas.items()
            if not janela or agora - janela[-1] > JANELA_SEGUNDOS
        ]
        for origem in vazias:
            del self._janelas[origem]
        logger.debug("limitador: %d origens podadas", len(vazias))


def origem_da_requisicao(scope: dict[str, Any]) -> str:
    """IP de origem, respeitando X-Forwarded-For quando há proxy na frente.

    Atrás de um proxy reverso, o IP do socket é o do proxy: todos os usuários
    apareceriam como a mesma origem e um só consumiria o limite de todos.

    Só confia no cabeçalho quando o socket vem de loopback (o proxy local), e
    usa a entrada mais à direita, a que o proxy acrescentou. As da esquerda
    vêm do cliente: usá-las deixaria qualquer um trocar de origem a cada
    requisição e fugir do limite.
    """
    cliente = scope.get("client")
    ip_socket = str(cliente[0]) if cliente else "desconhecido"
    if ip_socket not in _LOOPBACK:
        return ip_socket
    cabecalhos: dict[str, str] = {
        k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []
    }
    encaminhado = cabecalhos.get("x-forwarded-for")
    if encaminhado:
        return encaminhado.split(",")[-1].strip()
    return ip_socket


_LOOPBACK = {"127.0.0.1", "::1"}
