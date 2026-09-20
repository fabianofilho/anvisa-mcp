"""Fontes de dados abertos da Anvisa e seus parsers.

NENHUMA URL É INVENTADA AQUI. Enquanto uma fonte não for confirmada abrindo a
página do dataset, ``url`` fica ``None`` e o sync falha com mensagem explícita,
em vez de bater num endpoint que pode não existir.

Para confirmar uma fonte, use o portal de dados abertos do governo federal
(dados.gov.br), filtrando pela organização Anvisa, abra a página do conjunto de
dados e copie a URL do recurso (CSV) publicado. Preencha ``url``, ajuste o
parser ao cabeçalho real do arquivo e rode ``anvisa-cli sync``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


class FonteNaoConfigurada(RuntimeError):
    """A URL do dataset ainda não foi confirmada — não há o que baixar."""


@dataclass(frozen=True)
class FonteDados:
    """Um dataset público da Anvisa.

    Attributes:
        chave: identificador interno, também o nome da tabela de destino.
        descricao: o que o dataset contém, em português.
        url: URL do recurso CSV. ``None`` enquanto não confirmada.
        parser: normaliza as linhas brutas para o schema interno.
        onde_encontrar: pista para quem for confirmar a URL depois.
    """

    chave: str
    descricao: str
    url: str | None
    parser: Callable[[Iterable[dict[str, str]]], list[dict[str, Any]]]
    onde_encontrar: str

    def exigir_url(self) -> str:
        if not self.url:
            raise FonteNaoConfigurada(
                f"A fonte {self.chave!r} não tem URL confirmada. {self.onde_encontrar} "
                "Depois preencha 'url' em data/sources.py."
            )
        return self.url


def _texto(linha: dict[str, str], *nomes: str) -> str | None:
    """Primeiro valor não vazio entre as colunas candidatas (cabeçalhos variam)."""
    for nome in nomes:
        valor = (linha.get(nome) or "").strip()
        if valor:
            return valor
    return None


def _data(linha: dict[str, str], *nomes: str) -> date | None:
    bruto = _texto(linha, *nomes)
    if not bruto:
        return None
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(bruto, formato).date()
        except ValueError:
            continue
    return None


def parse_medicamentos(linhas: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Normaliza o CSV de medicamentos para o schema da tabela ``medicamentos``.

    Os nomes de coluna são candidatos: ajuste à planilha real ao confirmar a fonte.
    """
    registros: list[dict[str, Any]] = []
    for linha in linhas:
        numero = _texto(linha, "NUMERO_REGISTRO", "REGISTRO", "numero_registro")
        nome = _texto(linha, "NOME_PRODUTO", "PRODUTO", "nome_produto")
        if not numero or not nome:
            continue
        registros.append(
            {
                "numero_registro": numero,
                "nome_produto": nome,
                "principio_ativo": _texto(
                    linha, "PRINCIPIO_ATIVO", "SUBSTANCIA", "principio_ativo"
                ),
                "empresa_detentora": _texto(linha, "EMPRESA_DETENTORA", "EMPRESA", "razao_social"),
                "situacao": _texto(linha, "SITUACAO_REGISTRO", "SITUACAO", "situacao"),
                "data_situacao": _data(linha, "DATA_SITUACAO", "DATA_VENCIMENTO", "data_situacao"),
                "categoria": _texto(linha, "CATEGORIA_REGULATORIA", "CATEGORIA", "categoria"),
            }
        )
    return registros


def parse_dispositivos(linhas: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Normaliza o CSV de produtos para saúde para ``dispositivos_medicos``."""
    registros: list[dict[str, Any]] = []
    for linha in linhas:
        numero = _texto(linha, "NUMERO_REGISTRO", "REGISTRO", "numero_registro")
        nome = _texto(linha, "NOME_PRODUTO", "PRODUTO", "nome_produto")
        if not numero or not nome:
            continue
        registros.append(
            {
                "numero_registro": numero,
                "nome_produto": nome,
                "empresa_detentora": _texto(linha, "EMPRESA_DETENTORA", "EMPRESA", "razao_social"),
                "classe_risco": _texto(linha, "CLASSE_RISCO", "CLASSE", "classe_risco"),
                "situacao": _texto(linha, "SITUACAO_REGISTRO", "SITUACAO", "situacao"),
                "data_registro": _data(linha, "DATA_REGISTRO", "DATA_PUBLICACAO", "data_registro"),
                "descricao": _texto(linha, "DESCRICAO", "APRESENTACAO", "INDICACAO", "descricao"),
            }
        )
    return registros


MEDICAMENTOS = FonteDados(
    chave="medicamentos",
    descricao=(
        "Consulta de medicamentos registrados "
        "(situação do registro, empresa, princípio ativo)"
    ),
    url=None,
    parser=parse_medicamentos,
    onde_encontrar=(
        "Procure em dados.gov.br o conjunto de dados de medicamentos registrados "
        "publicado pela Anvisa e copie a URL do recurso CSV."
    ),
)

DISPOSITIVOS_MEDICOS = FonteDados(
    chave="dispositivos_medicos",
    descricao=(
        "Registro de produtos para saúde (dispositivos médicos Classe I-IV, RDC 657/2022); "
        "é aqui que SaMD aparece"
    ),
    url=None,
    parser=parse_dispositivos,
    onde_encontrar=(
        "Procure em dados.gov.br o conjunto de dados de produtos para saúde registrados "
        "publicado pela Anvisa e copie a URL do recurso CSV."
    ),
)

FONTES: dict[str, FonteDados] = {
    MEDICAMENTOS.chave: MEDICAMENTOS,
    DISPOSITIVOS_MEDICOS.chave: DISPOSITIVOS_MEDICOS,
}
