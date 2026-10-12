"""Idade da base: quando a última coleta aconteceu e se já passou do esperado."""

from __future__ import annotations

from datetime import datetime, timedelta

# O sync roda uma vez por dia. Com folga para um dia de falha da fonte, acima
# disto a base provavelmente parou de ser atualizada.
IDADE_MAXIMA_ESPERADA = timedelta(hours=48)


def _mesmo_tipo_de_fuso(a: datetime, b: datetime) -> tuple[datetime, datetime]:
    """Deixa os dois conscientes de fuso, ou os dois ingenuos.

    Quando so um tem fuso, o ingenuo e lido como hora local, que e o que ele e:
    ambos nascem de "now()" ou de "current_timestamp" na mesma maquina.
    """
    if (a.tzinfo is None) == (b.tzinfo is None):
        return a, b
    return (a.astimezone() if a.tzinfo is None else a), (b.astimezone() if b.tzinfo is None else b)


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
    # Os dois lados com fuso ou os dois sem: subtrair misturado levanta
    # TypeError. "ultima_coleta" passou a devolver com fuso para o campo
    # "coletado_em" ser RFC 3339, e quem chama daqui pode passar "agora"
    # ingenuo, então a conversao e feita aqui, dos dois lados.
    referencia, coletado_em = _mesmo_tipo_de_fuso(referencia, coletado_em)
    if referencia - coletado_em <= IDADE_MAXIMA_ESPERADA:
        return None
    return (
        f"A última coleta desta base foi em {coletado_em:%d/%m/%Y %H:%M}, há mais de "
        f"{int(IDADE_MAXIMA_ESPERADA.total_seconds() // 3600)} horas. A coleta diária "
        "pode ter falhado: a situação mostrada pode estar desatualizada."
    )
