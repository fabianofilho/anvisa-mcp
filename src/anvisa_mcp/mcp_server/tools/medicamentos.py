"""Tool ``consultar_status_medicamento``.

Independente da tool de SaMD: não compartilha estado nem exige que ela rode antes.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from anvisa_mcp.store.db import conectar
from anvisa_mcp.store.queries import buscar_medicamentos

logger = logging.getLogger(__name__)

Fonte = Literal["duckdb", "mock"]


class RegistroMedicamento(BaseModel):
    """Um registro de medicamento na Anvisa."""

    numero_registro: str
    nome_produto: str
    principio_ativo: str | None = None
    empresa_detentora: str | None = None
    situacao: str | None = Field(
        default=None, description="deferido, indeferido, caducado, em análise"
    )
    data_situacao: date | None = None
    categoria: str | None = None


class RespostaMedicamentos(BaseModel):
    """Retorno estruturado da tool. O cliente MCP é quem formata o texto final."""

    termo_consultado: str
    fonte: Fonte = Field(
        description="'mock' = dado de exemplo, ainda não é registro real da Anvisa"
    )
    total: int
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

AVISO_MOCK = (
    "Dados de exemplo: a base local ainda não foi sincronizada com a Anvisa. "
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
    nome_ou_principio_ativo: str,
    *,
    caminho_db: str,
    limite: int = 20,
) -> RespostaMedicamentos:
    """Status do registro de um medicamento, por nome comercial ou princípio ativo.

    Devolve todos os resultados casados, não só o primeiro: grafias variam e a
    ambiguidade é do usuário resolver.
    """
    termo = nome_ou_principio_ativo.strip()
    if not termo:
        return RespostaMedicamentos(
            termo_consultado=termo,
            fonte="duckdb",
            total=0,
            resultados=[],
            aviso="Informe um nome comercial ou princípio ativo.",
        )

    try:
        with conectar(caminho_db) as conexao:
            linhas = buscar_medicamentos(conexao, termo, limite=limite)
    except Exception:  # noqa: BLE001 - base indisponível não pode derrubar a tool
        logger.exception("falha ao consultar o DuckDB; caindo para mock")
        linhas = []

    if linhas:
        return RespostaMedicamentos(
            termo_consultado=termo,
            fonte="duckdb",
            total=len(linhas),
            resultados=[RegistroMedicamento.model_validate(linha) for linha in linhas],
        )

    mock = _filtrar_mock(termo)
    return RespostaMedicamentos(
        termo_consultado=termo,
        fonte="mock",
        total=len(mock),
        resultados=[RegistroMedicamento.model_validate(linha) for linha in mock],
        aviso=AVISO_MOCK,
    )
