"""Confere se o modelo leu o registro ou descreveu o produto de memória.

Em uso real, o classificador acertou o veredito e errou a descrição: chamou um
teste rápido de "mangueira biológica" e um kit canulado de "eletrodo físico de
uso único". Nenhum desses termos está no registro. O veredito ("não usa IA")
estava certo, mas a justificativa é o mecanismo de controle da tool, e
justificativa inventada desarma justamente esse mecanismo.

Pedir no prompt para não inventar não resolve: um modelo pequeno diante de um
texto vago preenche a lacuna com o que parece plausível. Por isso ele passou a
declarar os trechos em que se baseou, e aqui esses trechos são conferidos contra
o texto que ele recebeu.

A comparação é tolerante a acento, caixa e espaço, e aceita o trecho quando a
maior parte das palavras dele aparece no registro: exigir a frase idêntica
reprovaria paráfrase honesta, e o alvo aqui é invenção, não estilo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Abaixo disso o trecho nao tem palavra suficiente para ser evidencia de nada.
_FRACAO_MINIMA = 0.6
_MIN_LETRAS = 4


@dataclass(frozen=True)
class Evidencia:
    """O que sobrou depois de conferir os trechos contra o texto."""

    confere: bool
    ausentes: tuple[str, ...]


def _normalizar(texto: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sem_acento.lower()).strip()


def _palavras(texto: str) -> list[str]:
    return [p for p in re.split(r"\W+", _normalizar(texto)) if len(p) >= _MIN_LETRAS]


def verificar(termos: list[str], texto_do_registro: str) -> Evidencia:
    """Quais trechos declarados não se sustentam no texto do registro.

    Lista vazia devolve ``confere=True``: não afirmar nada é honesto, e quem
    consome vê isso pela confiança baixa que o prompt pede nesse caso.
    """
    if not termos:
        return Evidencia(True, ())

    alvo = _normalizar(texto_do_registro)
    palavras_do_alvo = set(_palavras(texto_do_registro))
    ausentes: list[str] = []

    for termo in termos:
        if _normalizar(termo) in alvo:
            continue
        palavras = _palavras(termo)
        if not palavras:
            ausentes.append(termo)
            continue
        presentes = sum(1 for p in palavras if p in palavras_do_alvo)
        if presentes / len(palavras) < _FRACAO_MINIMA:
            ausentes.append(termo)

    return Evidencia(not ausentes, tuple(ausentes))
