"""Limitador de taxa para o modo connector.

Um connector aberto pode ser chamado por qualquer usuario do Claude. Sem teto,
uma unica sessao mal comportada consome a maquina que serve todo mundo.

**Por que sao dois limites, e nao um por IP.** Quando o Claude.ai chama um
connector remoto, as requisicoes chegam dos IPs da Anthropic, nao do usuario
final, a propria documentacao manda liberar as faixas deles no firewall. Um
limite por IP colocaria todos os usuarios do Claude no mesmo balde: ou e
restritivo e derruba todo mundo junto, ou e frouxo e nao protege nada.

Entao:

- o **limite global** e o que de fato protege a maquina, e e o teto que importa
  para trafego vindo do Claude;
- o **limite por origem** continua util contra quem chama o servidor direto,
  fora do Claude, e fica alto o suficiente para nao atrapalhar o uso normal.

Nenhum dos dois e defesa contra ataque distribuido. Sao para o caso comum de
cliente em laco.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

JANELA_SEGUNDOS = 60.0
# Acima disso, para de guardar IP novo: memoria nao pode crescer sem teto.
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
        """'global' ou 'origem', para o log dizer qual teto foi batido."""
        return self._motivo

    _motivo = "origem"

    def permitir(self, origem: str) -> bool:
        """True se a requisicao cabe nos dois limites. Registra quando cabe."""
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
                # Tabela cheia so de origens ativas: deixa passar em vez de
                # recusar cliente legitimo por limitacao nossa de memoria.
                return True
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
    """IP de origem, respeitando X-Forwarded-For quando ha proxy na frente.

    Atras de um proxy reverso, o IP do socket e o do proxy, todos os usuarios
    apareceriam como a mesma origem e um so consumiria o limite de todos.
    """
    cabecalhos: dict[str, str] = {
        k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []
    }
    encaminhado = cabecalhos.get("x-forwarded-for")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    cliente = scope.get("client")
    return str(cliente[0]) if cliente else "desconhecido"
