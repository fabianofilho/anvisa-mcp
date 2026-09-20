"""Fontes de dados abertos da Anvisa e seus parsers.

As duas URLs abaixo foram confirmadas em 2026-09-20 baixando os arquivos e lendo
o cabeçalho real — não foram deduzidas nem copiadas de documentação. O índice de
diretório fica em https://dados.anvisa.gov.br/dados/ e lista o que existe hoje.

Ambos os arquivos são ISO-8859-1 com separador ``;``.

Se uma URL sair do ar, deixe ``url=None`` em vez de chutar outra: o sync falha
com mensagem explicando como reconfirmar, que é melhor do que baixar o arquivo
errado em silêncio.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

BASE = "https://dados.anvisa.gov.br/dados"


class FonteNaoConfigurada(RuntimeError):
    """A URL do dataset não foi confirmada — não há o que baixar."""


@dataclass(frozen=True)
class FonteDados:
    """Um dataset público da Anvisa.

    Attributes:
        chave: identificador interno, também o nome da tabela de destino.
        descricao: o que o dataset contém, em português.
        url: URL do recurso CSV. ``None`` enquanto não confirmada.
        parser: normaliza as linhas brutas para o schema interno.
        onde_encontrar: pista para quem for reconfirmar a URL depois.
        encoding: codificação do arquivo publicado.
        delimitador: separador de campos do CSV.
        confirmada_em: quando alguém baixou o arquivo e conferiu o cabeçalho.
    """

    chave: str
    descricao: str
    url: str | None
    parser: Callable[[Iterable[dict[str, str]]], list[dict[str, Any]]]
    onde_encontrar: str
    encoding: str = "iso-8859-1"
    delimitador: str = ";"
    confirmada_em: str | None = None

    def exigir_url(self) -> str:
        if not self.url:
            raise FonteNaoConfigurada(
                f"A fonte {self.chave!r} não tem URL confirmada. {self.onde_encontrar} "
                "Depois preencha 'url' em data/sources.py."
            )
        return self.url


def _texto(linha: dict[str, str], *nomes: str) -> str | None:
    """Primeiro valor não vazio entre as colunas candidatas."""
    for nome in nomes:
        valor = (linha.get(nome) or "").strip()
        if valor:
            return valor
    return None


def _data(linha: dict[str, str], *nomes: str) -> date | None:
    """Data em qualquer um dos formatos que a Anvisa publica.

    Inclui ``MMAAAA`` sem separador, que aparece em DATA_VENCIMENTO_REGISTRO.
    """
    bruto = _texto(linha, *nomes)
    if not bruto:
        return None
    bruto = bruto.split(" ")[0]
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%Y", "%m%Y"):
        try:
            return datetime.strptime(bruto, formato).date()
        except ValueError:
            continue
    return None


def parse_medicamentos(linhas: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Normaliza DADOS_ABERTOS_MEDICAMENTOS.csv para a tabela ``medicamentos``.

    Cabeçalho real confirmado em 2026-09-20::

        TIPO_PRODUTO;NOME_PRODUTO;DATA_FINALIZACAO_PROCESSO;CATEGORIA_REGULATORIA;
        NUMERO_REGISTRO_PRODUTO;DATA_VENCIMENTO_REGISTRO;NUMERO_PROCESSO;
        CLASSE_TERAPEUTICA;EMPRESA_DETENTORA_REGISTRO;SITUACAO_REGISTRO;PRINCIPIO_ATIVO

    Linhas sem número de registro são descartadas: o arquivo inclui produtos
    notificados (baixo risco), que não têm registro e não respondem à pergunta
    "qual o status do registro".
    """
    registros: list[dict[str, Any]] = []
    for linha in linhas:
        numero = _texto(linha, "NUMERO_REGISTRO_PRODUTO", "NUMERO_REGISTRO", "REGISTRO")
        nome = _texto(linha, "NOME_PRODUTO", "PRODUTO")
        if not numero or not nome:
            continue
        registros.append(
            {
                "numero_registro": numero,
                "nome_produto": nome,
                "principio_ativo": _texto(linha, "PRINCIPIO_ATIVO", "SUBSTANCIA"),
                "empresa_detentora": _texto(
                    linha, "EMPRESA_DETENTORA_REGISTRO", "EMPRESA_DETENTORA", "EMPRESA"
                ),
                "situacao": _texto(linha, "SITUACAO_REGISTRO", "SITUACAO"),
                "data_situacao": _data(linha, "DATA_VENCIMENTO_REGISTRO", "DATA_SITUACAO"),
                "categoria": _texto(linha, "CATEGORIA_REGULATORIA", "CATEGORIA"),
            }
        )
    return registros


def parse_dispositivos(linhas: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Normaliza TA_PRODUTO_SAUDE_SITE.csv para ``dispositivos_medicos``.

    Cabeçalho real confirmado em 2026-09-20::

        NUMERO_REGISTRO_CADASTRO;NUMERO_PROCESSO;NOME_TECNICO;CLASSE_RISCO;
        NOME_COMERCIAL;CNPJ_DETENTOR_REGISTRO_CADASTRO;DETENTOR_REGISTRO_CADASTRO;
        NOME_FABRICANTE;NOME_PAIS_FABRIC;DT_PUB_REGISTRO_CADASTRO;
        VALIDADE_REGISTRO_CADASTRO;DT_ATUALIZACAO_DADO

    ``VALIDADE_REGISTRO_CADASTRO`` vem como "VIGENTE" ou como uma data de
    validade; o valor é guardado cru em ``situacao``.

    Este arquivo não traz descrição livre do produto. O texto que alimenta a
    classificação de IA é montado de NOME_TECNICO, NOME_COMERCIAL e fabricante —
    é pouco, e a confiança devolvida pela classificação reflete isso.
    """
    registros: list[dict[str, Any]] = []
    for linha in linhas:
        numero = _texto(linha, "NUMERO_REGISTRO_CADASTRO", "NUMERO_REGISTRO", "REGISTRO")
        comercial = _texto(linha, "NOME_COMERCIAL")
        tecnico = _texto(linha, "NOME_TECNICO")
        nome = comercial or tecnico
        if not numero or not nome:
            continue

        fabricante = _texto(linha, "NOME_FABRICANTE")
        partes = [
            f"Nome técnico: {tecnico}" if tecnico else None,
            f"Nome comercial: {comercial}" if comercial else None,
            f"Fabricante: {fabricante}" if fabricante else None,
        ]
        registros.append(
            {
                "numero_registro": numero,
                "nome_produto": nome,
                "empresa_detentora": _texto(
                    linha, "DETENTOR_REGISTRO_CADASTRO", "EMPRESA_DETENTORA", "EMPRESA"
                ),
                "classe_risco": _texto(linha, "CLASSE_RISCO", "SG_RISCO_PRODUTO", "CLASSE"),
                "situacao": _texto(linha, "VALIDADE_REGISTRO_CADASTRO", "SITUACAO_REGISTRO"),
                "data_registro": _data(linha, "DT_PUB_REGISTRO_CADASTRO", "DATA_REGISTRO"),
                "descricao": ". ".join(p for p in partes if p) or None,
            }
        )
    return registros


MEDICAMENTOS = FonteDados(
    chave="medicamentos",
    descricao=(
        "Medicamentos registrados: nome, princípio ativo, empresa detentora, "
        "situação do registro e categoria regulatória (extraído do Datavisa)"
    ),
    url=f"{BASE}/DADOS_ABERTOS_MEDICAMENTOS.csv",
    parser=parse_medicamentos,
    onde_encontrar=(
        "O índice https://dados.anvisa.gov.br/dados/ lista os arquivos publicados; "
        "o dicionário de dados é Documentacao_e_Dicionario_de_Dados_MEDICAMENTOS.pdf."
    ),
    confirmada_em="2026-09-20",
)

DISPOSITIVOS_MEDICOS = FonteDados(
    chave="dispositivos_medicos",
    descricao=(
        "Produtos para saúde registrados (dispositivos médicos Classe I-IV): "
        "nome técnico e comercial, classe de risco, detentor e data de publicação"
    ),
    url=f"{BASE}/TA_PRODUTO_SAUDE_SITE.csv",
    parser=parse_dispositivos,
    onde_encontrar=(
        "O índice https://dados.anvisa.gov.br/dados/ lista os arquivos publicados; "
        "o dicionário de dados é Documentacao_e_Dicionario_de_Dados_PRODUTO_SAUDE.pdf."
    ),
    confirmada_em="2026-09-20",
)

FONTES: dict[str, FonteDados] = {
    MEDICAMENTOS.chave: MEDICAMENTOS,
    DISPOSITIVOS_MEDICOS.chave: DISPOSITIVOS_MEDICOS,
}
