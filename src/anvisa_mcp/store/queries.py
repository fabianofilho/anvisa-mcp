"""Queries SQL reutilizáveis sobre o DuckDB local."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import duckdb


def buscar_medicamentos(
    conexao: duckdb.DuckDBPyConnection,
    termo: str,
    *,
    limite: int = 20,
) -> list[dict[str, Any]]:
    """Busca por nome comercial OU princípio ativo, tolerante a grafia.

    Casa por substring sem acento e sem caixa. Registros ativos vêm primeiro:
    dois terços da base são inativos, e quem pergunta "qual o status de X"
    normalmente quer saber do que ainda vale. Depois, nomes mais curtos, que
    tendem a ser o produto em si e não uma apresentação específica.
    """
    padrao = f"%{termo.strip()}%"
    return _para_dicts(
        conexao.execute(
            """
            SELECT numero_registro, nome_produto, principio_ativo, empresa_detentora,
                   situacao, data_situacao, categoria
            FROM medicamentos
            WHERE strip_accents(lower(nome_produto)) LIKE strip_accents(lower(?))
               OR strip_accents(lower(coalesce(principio_ativo, ''))) LIKE strip_accents(lower(?))
            ORDER BY (lower(coalesce(situacao, '')) = 'ativo') DESC,
                     length(nome_produto), nome_produto
            LIMIT ?
            """,
            [padrao, padrao, limite],
        )
    )


# Termos que indicam que o registro pode ser software. SaMD é raro: dos 1.832
# registros Classe III/IV do último ano, 13 mencionam software. Sem este filtro,
# os primeiros N por data são cânulas e parafusos, e a busca por SaMD nunca
# alcança um candidato — gastando uma chamada de LLM em cada um deles.
TERMOS_SOFTWARE = (
    "SOFTWARE",
    "ALGORITMO",
    "INTELIG",  # inteligência, intelligent
    "APRENDIZADO",
    "MACHINE LEARNING",
    "APLICATIVO",
    "APP ",
    "PROGRAMA DE COMPUTADOR",
    "CAD",  # computer-aided detection/diagnosis
    "PROCESSAMENTO DE IMAGEM",
    "ANALISE DE IMAGEM",
    "ANÁLISE DE IMAGEM",
)


def dispositivos_no_periodo(
    conexao: duckdb.DuckDBPyConnection,
    *,
    dias: int,
    classes: tuple[str, ...] = ("III", "IV"),
    limite: int = 200,
    apenas_software: bool = False,
) -> list[dict[str, Any]]:
    """Dispositivos registrados nos últimos ``dias``, nas classes de risco dadas.

    Com ``apenas_software``, mantém só registros cujo texto sugere software. É um
    filtro por palavra-chave, então troca recall por custo: um produto que use IA
    sem dizer nenhum desses termos fica de fora. Quem chama precisa dizer isso a
    quem perguntou.
    """
    corte = date.today() - timedelta(days=dias)
    marcadores = ", ".join("?" for _ in classes)
    parametros: list[Any] = [corte, *classes]

    filtro_software = ""
    if apenas_software:
        condicoes = " OR ".join(
            "upper(coalesce(descricao, '') || ' ' || nome_produto) LIKE ?" for _ in TERMOS_SOFTWARE
        )
        filtro_software = f"AND ({condicoes})"
        parametros.extend(f"%{termo}%" for termo in TERMOS_SOFTWARE)

    parametros.append(limite)
    return _para_dicts(
        conexao.execute(
            f"""
            SELECT numero_registro, nome_produto, empresa_detentora, classe_risco,
                   situacao, data_registro, descricao
            FROM dispositivos_medicos
            WHERE data_registro >= ?
              AND classe_risco IN ({marcadores})
              {filtro_software}
            -- numero_registro desempata: sem ele, registros com a mesma data
            -- saem em ordem arbitrária e o LIMIT devolve um conjunto diferente
            -- a cada chamada, furando o cache de classificação.
            ORDER BY data_registro DESC, numero_registro
            LIMIT ?
            """,
            parametros,
        )
    )


def classificacao_em_cache(
    conexao: duckdb.DuckDBPyConnection,
    numero_registro: str,
) -> dict[str, Any] | None:
    """Classificação já calculada para esse registro, se houver."""
    linhas = _para_dicts(
        conexao.execute(
            """
            SELECT numero_registro, usa_ia, confianca, justificativa, modelo, classificado_em
            FROM classificacoes_samd
            WHERE numero_registro = ?
            """,
            [numero_registro],
        )
    )
    return linhas[0] if linhas else None


def gravar_classificacao(
    conexao: duckdb.DuckDBPyConnection,
    *,
    numero_registro: str,
    usa_ia: bool,
    confianca: float,
    justificativa: str,
    modelo: str,
) -> None:
    """Grava (ou atualiza) a classificação de um registro."""
    conexao.execute(
        """
        INSERT INTO classificacoes_samd
            (numero_registro, usa_ia, confianca, justificativa, modelo)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (numero_registro) DO UPDATE SET
            usa_ia = excluded.usa_ia,
            confianca = excluded.confianca,
            justificativa = excluded.justificativa,
            modelo = excluded.modelo,
            classificado_em = now()
        """,
        [numero_registro, usa_ia, confianca, justificativa, modelo],
    )


def _para_dicts(resultado: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    colunas = [d[0] for d in resultado.description or []]
    return [dict(zip(colunas, linha, strict=True)) for linha in resultado.fetchall()]
