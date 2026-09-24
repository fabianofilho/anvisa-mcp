"""Queries SQL reutilizáveis sobre o DuckDB local."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import duckdb


def _visto_na_ultima_coleta(tabela: str) -> str:
    """Expressão SQL: esta linha apareceu no arquivo da coleta mais recente?

    O upsert é o único jeito de uma linha ganhar ``atualizado_em = now()``, e o
    ``now()`` do DuckDB é constante dentro da transação, então todas as linhas
    presentes no arquivo compartilham o mesmo timestamp, e o máximo da tabela é
    o horário da última coleta.

    Isso importa porque o upsert **não remove**: um registro que a Anvisa tirou
    da publicação continua na base com a situação antiga. Sem esta marca, a tool
    devolveria um registro possivelmente cancelado como se ainda valesse, que é
    o pior erro que ela pode cometer.
    """
    return f"(atualizado_em >= (SELECT max(atualizado_em) FROM {tabela})) AS visto_na_ultima_coleta"


def _escapar_like(texto: str) -> str:
    """Neutraliza os curingas do LIKE no termo digitado.

    Sem isso, buscar "%" ou "_" casa com a base inteira (32.752 linhas numa
    chamada), e um nome com sublinhado casaria qualquer caractere na posição.
    """
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_SO_PONTUACAO_DE_NUMERO = re.compile(r"^[\d.\-/\s]+$")


def numeros_de_registro_candidatos(termo: str) -> list[str]:
    """Números de registro que o termo pode estar citando.

    A base guarda o número só com dígitos (9, para o produto). Quem pergunta
    costuma copiar do rótulo com pontos ("1.0582.0010") ou o número de 13
    dígitos da apresentação, cujos 9 primeiros são o registro do produto.
    Termo com letra não é número de registro: devolve lista vazia.
    """
    if not _SO_PONTUACAO_DE_NUMERO.match(termo.strip()):
        return []
    digitos = re.sub(r"\D", "", termo)
    if not digitos:
        return []
    candidatos = [digitos]
    if len(digitos) == 13:
        candidatos.append(digitos[:9])
    return candidatos


def _condicao_medicamento(termo: str) -> tuple[str, list[Any]]:
    """A mesma condição na busca e na contagem.

    Se divergirem, o total deixa de descrever o conjunto que foi paginado.
    Casa por substring no nome comercial ou no princípio ativo e, quando o termo
    tem cara de número, também pelo número de registro exato.
    """
    padrao = f"%{_escapar_like(termo.strip())}%"
    condicao = (
        "strip_accents(lower(nome_produto)) LIKE strip_accents(lower(?)) ESCAPE '\\' "
        "OR strip_accents(lower(coalesce(principio_ativo, ''))) "
        "LIKE strip_accents(lower(?)) ESCAPE '\\'"
    )
    parametros: list[Any] = [padrao, padrao]
    numeros = numeros_de_registro_candidatos(termo)
    if numeros:
        condicao += f" OR numero_registro IN ({', '.join('?' for _ in numeros)})"
        parametros.extend(numeros)
    return condicao, parametros


def buscar_medicamentos(
    conexao: duckdb.DuckDBPyConnection,
    termo: str,
    *,
    limite: int = 20,
) -> list[dict[str, Any]]:
    """Busca por nome comercial, princípio ativo ou número de registro.

    Nome e princípio ativo casam por substring sem acento e sem caixa. Registros
    ativos vêm primeiro: dois terços da base são inativos, e quem pergunta "qual
    o status de X" normalmente quer saber do que ainda vale. Depois, nomes mais
    curtos, que tendem a ser o produto em si e não uma apresentação específica.
    """
    condicao, parametros = _condicao_medicamento(termo)
    return _para_dicts(
        conexao.execute(
            f"""
            SELECT numero_registro, nome_produto, principio_ativo, empresa_detentora,
                   situacao, data_situacao, categoria,
                   {_visto_na_ultima_coleta("medicamentos")}
            FROM medicamentos
            WHERE {condicao}
            ORDER BY (lower(coalesce(situacao, '')) = 'ativo') DESC,
                     length(nome_produto), nome_produto
            LIMIT ?
            """,
            [*parametros, limite],
        )
    )


def contar_medicamentos(conexao: duckdb.DuckDBPyConnection, termo: str) -> int:
    """Quantos registros casam no total, ignorando o limite de exibição.

    Sem este número, a resposta diz "20 resultados" tanto para um termo com 20
    registros quanto para um com 557, e quem lê conclui que são 20 no país.
    """
    condicao, parametros = _condicao_medicamento(termo)
    linha = conexao.execute(
        f"SELECT count(*) FROM medicamentos WHERE {condicao}", parametros
    ).fetchone()
    return int(linha[0]) if linha else 0


def total_de_linhas(conexao: duckdb.DuckDBPyConnection, tabela: str) -> int:
    """Linhas da tabela, ou 0 quando ela nem existe.

    Separa "a base nunca foi sincronizada" (dado de exemplo é aceitável, com
    aviso) de "a base tem dados e nada casou" (a resposta certa é lista vazia).
    """
    try:
        linha = conexao.execute(f"SELECT count(*) FROM {tabela}").fetchone()
    except duckdb.CatalogException:
        return 0
    return int(linha[0]) if linha else 0


def ultima_coleta(conexao: duckdb.DuckDBPyConnection, tabela: str) -> datetime | None:
    """Quando a tabela foi atualizada pela última coleta.

    É o ``max(atualizado_em)``: o upsert grava ``now()`` em toda linha presente
    no arquivo, então esse máximo é a hora da coleta mais recente que trouxe
    dados. Quem consulta precisa disso para saber se o sync parou.
    """
    try:
        linha = conexao.execute(f"SELECT max(atualizado_em) FROM {tabela}").fetchone()
    except duckdb.CatalogException:
        return None
    if not linha or linha[0] is None:
        return None
    valor = linha[0]
    return valor if isinstance(valor, datetime) else None


# Termos que indicam que o registro pode ser software. SaMD é raro: dos 1.832
# registros Classe III/IV do último ano, 13 mencionam software. Sem este filtro,
# os primeiros N por data são cânulas e parafusos, e a busca por SaMD nunca
# alcança um candidato, gastando uma chamada de LLM em cada um deles.
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
                   situacao, data_registro, descricao,
                   {_visto_na_ultima_coleta("dispositivos_medicos")}
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


def contar_dispositivos_no_periodo(
    conexao: duckdb.DuckDBPyConnection,
    *,
    dias: int,
    classes: tuple[str, ...] = ("III", "IV"),
    apenas_software: bool = False,
) -> int:
    """Quantos dispositivos existem na janela, ignorando o limite de análise.

    A tool classifica no máximo ``limite`` registros por chamada, e sem este
    número a resposta não distingue "havia 12 no período" de "havia 900 e eu vi
    os 50 mais recentes". A segunda leitura, tratada como a primeira, vira
    contagem errada de quantos SaMD foram registrados no período.
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

    linha = conexao.execute(
        f"""
        SELECT count(*) FROM dispositivos_medicos
        WHERE data_registro >= ?
          AND classe_risco IN ({marcadores})
          {filtro_software}
        """,
        parametros,
    ).fetchone()
    return int(linha[0]) if linha else 0


_CACHE_COLUNAS = (
    "numero_registro, usa_ia, confianca, justificativa, modelo, classificado_em, evidencia_confere"
)
# Sem a coluna nova, que so existe depois da migracao rodar.
_CACHE_COLUNAS_ANTIGAS = (
    "numero_registro, usa_ia, confianca, justificativa, modelo, classificado_em"
)


def classificacao_em_cache(
    conexao: duckdb.DuckDBPyConnection,
    numero_registro: str,
) -> dict[str, Any] | None:
    """Classificação já calculada para esse registro, se houver.

    Tolera base anterior à coluna ``evidencia_confere``. Sem isso, quem
    atualizasse o código antes de rodar a coleta veria a consulta falhar e a tool
    cair para dados de exemplo, que é a pior forma de errar aqui: a resposta
    continua vindo, com cara de real.
    """
    for colunas in (_CACHE_COLUNAS, _CACHE_COLUNAS_ANTIGAS):
        try:
            linhas = _para_dicts(
                conexao.execute(
                    f"SELECT {colunas} FROM classificacoes_samd WHERE numero_registro = ?",
                    [numero_registro],
                )
            )
        except duckdb.Error:
            continue
        return linhas[0] if linhas else None
    return None


def dispositivos_com_veredito_sem_evidencia(
    conexao: duckdb.DuckDBPyConnection, *, limite: int
) -> list[dict[str, Any]]:
    """Dispositivos cujo veredito em cache é anterior à checagem de evidência.

    Não passa pelo filtro de software nem por janela de datas: parte desses
    vereditos veio de consultas com ``apenas_software=False``, e a varredura da
    coleta, que usa o filtro, nunca os alcançaria.
    """
    return _para_dicts(
        conexao.execute(
            f"""
            SELECT d.numero_registro, d.nome_produto, d.empresa_detentora, d.classe_risco,
                   d.situacao, d.data_registro, d.descricao,
                   {_visto_na_ultima_coleta("dispositivos_medicos")}
            FROM classificacoes_samd c
            JOIN dispositivos_medicos d USING (numero_registro)
            WHERE c.evidencia_confere IS NULL
            ORDER BY d.data_registro DESC, d.numero_registro
            LIMIT ?
            """,
            [limite],
        )
    )


def gravar_classificacao(
    conexao: duckdb.DuckDBPyConnection,
    *,
    numero_registro: str,
    usa_ia: bool,
    confianca: float,
    justificativa: str,
    modelo: str,
    evidencia_confere: bool | None = None,
) -> None:
    """Grava (ou atualiza) a classificação de um registro."""
    conexao.execute(
        """
        INSERT INTO classificacoes_samd
            (numero_registro, usa_ia, confianca, justificativa, modelo, evidencia_confere)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (numero_registro) DO UPDATE SET
            usa_ia = excluded.usa_ia,
            confianca = excluded.confianca,
            justificativa = excluded.justificativa,
            modelo = excluded.modelo,
            evidencia_confere = excluded.evidencia_confere,
            classificado_em = now()
        """,
        [numero_registro, usa_ia, confianca, justificativa, modelo, evidencia_confere],
    )


def _para_dicts(resultado: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    colunas = [d[0] for d in resultado.description or []]
    return [dict(zip(colunas, linha, strict=True)) for linha in resultado.fetchall()]


def ausentes_na_ultima_coleta(conexao: duckdb.DuckDBPyConnection, tabela: str) -> int:
    """Quantos registros da base não apareceram no arquivo da última coleta.

    Um número diferente de zero não é erro: é a Anvisa tendo removido registros
    da publicação, provavelmente por cancelamento. Mas é informação que precisa
    chegar a quem consulta.
    """
    linha = conexao.execute(
        f"SELECT count(*) FROM {tabela} "
        f"WHERE atualizado_em < (SELECT max(atualizado_em) FROM {tabela})"
    ).fetchone()
    return int(linha[0]) if linha else 0
