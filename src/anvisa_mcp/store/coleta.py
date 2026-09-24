"""Idade da base: quando a última coleta aconteceu e se já passou do esperado."""

from __future__ import annotations

from datetime import datetime, timedelta

# O sync roda uma vez por dia. Com folga para um dia de falha da fonte, acima
# disto a base provavelmente parou de ser atualizada.
IDADE_MAXIMA_ESPERADA = timedelta(hours=48)


def aviso_de_coleta_antiga(
    coletado_em: datetime | None, *, agora: datetime | None = None
) -> str | None:
    """Aviso quando a última coleta é mais velha que o esperado, senão None.

    Não há canal externo de alerta quando o sync falha: o connector segue
    servindo a base anterior. Esta é a forma de quem consulta perceber.
    """
    if coletado_em is None:
        return None
    referencia = agora or datetime.now()
    if referencia - coletado_em <= IDADE_MAXIMA_ESPERADA:
        return None
    return (
        f"A última coleta desta base foi em {coletado_em:%d/%m/%Y %H:%M}, há mais de "
        f"{int(IDADE_MAXIMA_ESPERADA.total_seconds() // 3600)} horas. A coleta diária "
        "pode ter falhado: a situação mostrada pode estar desatualizada."
    )
