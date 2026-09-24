"""Tool ``consultar_status_medicamento``.

Independente da tool de SaMD: não compartilha estado nem exige que ela rode antes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from anvisa_mcp.store.coleta import aviso_de_coleta_antiga
from anvisa_mcp.store.db import BaseIndisponivel, conectar
from anvisa_mcp.store.queries import (
    buscar_medicamentos,
    contar_medicamentos,
    total_de_linhas,
    ultima_coleta,
)

logger = logging.getLogger(__name__)

Fonte = Literal["duckdb", "mock"]


class RegistroMedicamento(BaseModel):
    """Um registro de medicamento na Anvisa."""

    numero_registro: str
    nome_produto: str
    principio_ativo: str | None = None
    empresa_detentora: str | None = None
    situacao: str | None = Field(
        default=None,
        description=(
            "Como o dataset aberto da Anvisa entrega: 'Ativo' ou 'Inativo'. O arquivo "
            "não distingue cancelado, caducado ou vencido dentro de 'Inativo'."
        ),
    )
    data_situacao: date | None = Field(
        default=None,
        description=(
            "Data de vencimento do registro, do campo DATA_VENCIMENTO_REGISTRO do "
            "dataset da Anvisa. Não é a data em que a situação atual foi decidida."
        ),
    )
    categoria: str | None = None
    visto_na_ultima_coleta: bool = Field(
        default=True,
        description=(
            "False quando o registro não apareceu no arquivo da última coleta, "
            "a situação mostrada pode estar desatualizada"
        ),
    )


class RespostaMedicamentos(BaseModel):
    """Retorno estruturado da tool. O cliente MCP é quem formata o texto final."""

    termo_consultado: str
    fonte: Fonte = Field(
        description="'mock' = dado de exemplo, ainda não é registro real da Anvisa"
    )
    coletado_em: datetime | None = Field(
        default=None,
        description=(
            "Quando a base local foi atualizada pela última vez a partir do arquivo da "
            "Anvisa (horário local do servidor). None quando a resposta é dado de exemplo."
        ),
    )
    total: int = Field(
        description=(
            "Quantos registros casam com o termo na base inteira, não quantos vieram "
            "nesta resposta. Um princípio ativo comum tem centenas, um por detentor "
            "e apresentação."
        )
    )
    retornados: int = Field(
        default=0, description="Quantos registros vieram em 'resultados', no máximo 'limite'"
    )
    truncado: bool = Field(
        default=False,
        description=(
            "True quando total > retornados. Os que vieram são os ativos e de nome "
            "mais curto; os demais existem e não estão aqui. Não conte os resultados "
            "para responder 'quantos registros existem': use 'total'."
        ),
    )
    resultados: list[RegistroMedicamento]
    aviso: str | None = None


_MOCK: list[dict[str, Any]] = [
    {
        "numero_registro": "1.0000.0001",
        "nome_produto": "DIPIRONA MONOIDRATADA (EXEMPLO MOCK)",
        "principio_ativo": "dipirona monoidratada",
        "empresa_detentora": "LABORATÓRIO EXEMPLO LTDA",
        "situacao": "válido",
        "data_situacao": date(2030, 1, 1),
        "categoria": "genérico",
    },
    {
        "numero_registro": "1.0000.0002",
        "nome_produto": "DIPIRONA SÓDICA SOLUÇÃO (EXEMPLO MOCK)",
        "principio_ativo": "dipirona sódica",
        "empresa_detentora": "OUTRO LABORATÓRIO S.A.",
        "situacao": "caducado",
        "data_situacao": date(2021, 6, 30),
        "categoria": "similar",
    },
]

AVISO_AUSENTE_NA_FONTE = (
    "Um ou mais registros abaixo NÃO apareceram na última publicação da Anvisa "
    "(visto_na_ultima_coleta=false). A base guarda o que foi visto por último e não "
    "remove nada, então a situação mostrada pode estar desatualizada, registro que sai "
    "da publicação costuma ter sido cancelado. Confira no portal oficial antes de usar."
)

AVISO_MOCK = (
    "Dados de exemplo: a base local ainda não foi sincronizada com a Anvisa. "
    "Rode 'anvisa-cli sync'. Não use como informação regulatória."
)
AVISO_BASE_TRAVADA = (
    "Dados de exemplo: a base local existe mas não pôde ser lida agora, "
    "provavelmente há um sync em andamento. Tente de novo em alguns minutos. "
    "Não use como informação regulatória."
)


def _filtrar_mock(termo: str) -> list[dict[str, Any]]:
    alvo = termo.strip().lower()
    return [
        registro
        for registro in _MOCK
        if alvo in registro["nome_produto"].lower()
        or alvo in (registro["principio_ativo"] or "").lower()
    ] or _MOCK


async def consultar_status_medicamento(
    termo: str,
    *,
    caminho_db: str,
    limite: int = 20,
) -> RespostaMedicamentos:
    """Status do registro de um medicamento.

    ``termo`` é nome comercial, princípio ativo ou número de registro. Devolve
    todos os resultados casados, não só o primeiro: grafias variam e a
    ambiguidade é do usuário resolver. Dado de exemplo (``fonte='mock'``) só
    aparece quando a base não existe, está vazia ou não pôde ser aberta; com a
    base populada, termo sem correspondência devolve lista vazia.
    """
    termo = termo.strip()
    if not termo:
        return RespostaMedicamentos(
            termo_consultado=termo,
            fonte="duckdb",
            total=0,
            resultados=[],
            aviso="Informe um nome comercial, princípio ativo ou número de registro.",
        )

    motivo = AVISO_MOCK
    total_na_base = 0
    linhas: list[dict[str, Any]] = []
    coletado_em: datetime | None = None
    base_populada = False
    try:
        with conectar(caminho_db, somente_leitura=True) as conexao:
            if total_de_linhas(conexao, "medicamentos") > 0:
                base_populada = True
                linhas = buscar_medicamentos(conexao, termo, limite=limite)
                total_na_base = contar_medicamentos(conexao, termo)
                coletado_em = ultima_coleta(conexao, "medicamentos")
    except FileNotFoundError:
        pass
    except BaseIndisponivel as erro:
        # A base tem dados, mas está travada. Dizer "não sincronizada" seria
        # falso; o aviso precisa nomear a causa real.
        logger.warning("base local indisponível: %s", erro)
        motivo = AVISO_BASE_TRAVADA
    except Exception:  # noqa: BLE001 - nenhuma falha de base pode derrubar a tool
        logger.exception("falha ao consultar o DuckDB")
        motivo = AVISO_BASE_TRAVADA

    if base_populada:
        # Base com dados: a resposta vem dela, mesmo vazia. Cair para o exemplo
        # aqui devolveria um medicamento fictício como se fosse o procurado.
        resultados = [RegistroMedicamento.model_validate(linha) for linha in linhas]
        ausentes = sum(1 for r in resultados if not r.visto_na_ultima_coleta)
        truncado = total_na_base > len(resultados)
        avisos = []
        if not resultados:
            avisos.append(
                f"Nenhum registro casou com '{termo}' na base. A busca é por trecho do "
                "nome comercial ou do princípio ativo, ou pelo número de registro "
                "completo. Confira a grafia ou tente só parte do nome."
            )
        if ausentes:
            avisos.append(AVISO_AUSENTE_NA_FONTE)
        if truncado:
            avisos.append(
                f"Casaram {total_na_base} registros e estão aqui os {len(resultados)} "
                f"primeiros (ativos primeiro, depois nome mais curto). Um princípio "
                f"ativo comum tem um registro por detentor e apresentação, então "
                f"não conte os resultados para dizer quantos existem: use 'total'. "
                f"Para ver mais, aumente 'limite'."
            )
        antiga = aviso_de_coleta_antiga(coletado_em)
        if antiga:
            avisos.append(antiga)
        return RespostaMedicamentos(
            termo_consultado=termo,
            fonte="duckdb",
            coletado_em=coletado_em,
            total=total_na_base,
            retornados=len(resultados),
            truncado=truncado,
            resultados=resultados,
            aviso=" ".join(avisos) or None,
        )

    mock = _filtrar_mock(termo)
    return RespostaMedicamentos(
        termo_consultado=termo,
        fonte="mock",
        total=len(mock),
        retornados=len(mock),
        resultados=[RegistroMedicamento.model_validate(linha) for linha in mock],
        aviso=motivo,
    )
