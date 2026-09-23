"""Tool ``buscar_samd_recentes``.

Dispositivos médicos Classe III/IV registrados numa janela de tempo, classificados
quanto a uso de IA pela camada LLM. A classificação é heurística e vai marcada
como tal em cada item: nunca apresentar como fato regulatório.

Independente da tool de medicamentos.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from anvisa_mcp.llm.classify_samd import classificar_dispositivo
from anvisa_mcp.llm.evidencia import verificar as verificar_evidencia
from anvisa_mcp.llm.qwen_client import QwenClient, QwenIndisponivel
from anvisa_mcp.store.db import BaseIndisponivel, conectar
from anvisa_mcp.store.queries import (
    classificacao_em_cache,
    contar_dispositivos_no_periodo,
    dispositivos_no_periodo,
    gravar_classificacao,
)

logger = logging.getLogger(__name__)

Fonte = Literal["duckdb", "mock"]
OrigemClassificacao = Literal["llm", "cache", "indisponivel", "nao_classificado"]


class ClassificacaoIA(BaseModel):
    """Veredito heurístico sobre uso de IA, leia junto com a justificativa."""

    usa_ia: bool | None = Field(description="None quando não foi possível classificar")
    confianca: float | None = Field(default=None, ge=0.0, le=1.0)
    justificativa: str
    evidencia_confere: bool | None = Field(
        default=None,
        description=(
            "False quando o modelo citou como evidência algo que não está no texto do "
            "registro. O veredito pode continuar certo, mas a justificativa é o "
            "mecanismo de controle desta tool, e justificativa inventada o desarma: "
            "não repasse a descrição do produto sem conferir. None nas classificações "
            "feitas antes desta checagem existir."
        ),
    )
    origem: OrigemClassificacao
    heuristica: bool = Field(
        default=True,
        description="Sempre True: vem de LLM lendo texto livre, não de campo oficial da Anvisa",
    )


class DispositivoSaMD(BaseModel):
    """Um registro de dispositivo médico com sua classificação de IA."""

    numero_registro: str
    nome_produto: str
    empresa_detentora: str | None = None
    classe_risco: str | None = None
    situacao: str | None = Field(
        default=None,
        description=(
            "Campo VALIDADE_REGISTRO_CADASTRO do dataset, como a Anvisa entrega. Ele "
            "mistura duas coisas: para a maioria vem 'VIGENTE', para o resto vem uma "
            "data de validade. Quando for data, ela está também em "
            "'validade_registro', já estruturada."
        ),
    )
    validade_registro: date | None = Field(
        default=None,
        description=(
            "Data de validade do registro, quando 'situacao' traz uma data em vez de "
            "'VIGENTE'. None não significa registro sem prazo: significa que a fonte "
            "não deu data para este registro."
        ),
    )
    data_registro: date | None = None
    visto_na_ultima_coleta: bool = Field(
        default=True,
        description=(
            "False quando o registro não apareceu no arquivo da última coleta, "
            "a situação mostrada pode estar desatualizada"
        ),
    )
    classificacao: ClassificacaoIA


LIMIAR_INDETERMINADO = 0.5


class RespostaSaMD(BaseModel):
    """Retorno estruturado da tool."""

    dias: int
    apenas_com_ia: bool
    fonte: Fonte = Field(
        description="'mock' = dado de exemplo, ainda não é registro real da Anvisa"
    )
    total: int = Field(
        description=(
            "Quantos dispositivos Classe III/IV existem na janela na base, antes do "
            "limite de análise e do filtro de IA. É o universo, não o tamanho desta "
            "lista: não use a contagem dos resultados para dizer quantos foram "
            "registrados no período."
        )
    )
    analisados: int = Field(
        default=0,
        description=(
            "Quantos registros a tool efetivamente avaliou nesta chamada, no máximo "
            "'limite'. Os mais recentes primeiro."
        ),
    )
    retornados: int = Field(
        default=0, description="Quantos sobraram em 'resultados' depois do filtro de IA"
    )
    truncado: bool = Field(
        default=False,
        description=(
            "True quando total > analisados: existem registros no período que esta "
            "chamada nem chegou a olhar. Aumente 'limite' ou reduza 'dias'."
        ),
    )
    resultados: list[DispositivoSaMD]
    indeterminados: int = Field(
        default=0,
        description=(
            "Quantos registros ficaram de fora por terem sido classificados como sem IA "
            "com confiança baixa, ou seja, o texto não permitiu decidir, o que não é o "
            "mesmo que não usar IA"
        ),
    )
    aviso: str | None = None


_MOCK: list[dict[str, Any]] = [
    {
        "numero_registro": "8.0000.0001",
        "nome_produto": "DETECTOR DE NÓDULOS PULMONARES (EXEMPLO MOCK)",
        "empresa_detentora": "HEALTHTECH EXEMPLO LTDA",
        "classe_risco": "III",
        "situacao": "válido",
        "data_registro": date.today(),
        "descricao": (
            "Software para detecção automatizada de nódulos pulmonares em tomografia "
            "computadorizada, baseado em rede neural convolucional treinada."
        ),
    },
    {
        "numero_registro": "8.0000.0002",
        "nome_produto": "SISTEMA PACS HOSPITALAR (EXEMPLO MOCK)",
        "empresa_detentora": "IMAGEM EXEMPLO S.A.",
        "classe_risco": "III",
        "situacao": "válido",
        "data_registro": date.today(),
        "descricao": (
            "Sistema de gerenciamento de imagens médicas para armazenamento, distribuição "
            "e visualização de exames em rede hospitalar."
        ),
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
AVISO_HEURISTICA = (
    "A Anvisa não publica campo estruturado de uso de IA. A classificação abaixo é "
    "heurística, feita por LLM lendo o texto do registro: confira a justificativa."
)
AVISO_SEM_LLM = (
    "Este servidor não classifica sob demanda: devolve o que já está no cache, construído "
    "na coleta. Registros com origem='nao_classificado' não foram avaliados, não são "
    "'sem IA', são 'não se sabe'."
)

AVISO_FILTRO_SOFTWARE = (
    "Só foram analisados registros cujo texto sugere software (por palavra-chave). "
    "Um produto que use IA sem mencionar esses termos não aparece aqui. "
    "Para varrer todos os dispositivos do período, use apenas_software=False."
)


def _validade(situacao: str | None) -> date | None:
    """Data escondida no campo de situação.

    ``VALIDADE_REGISTRO_CADASTRO`` vem "VIGENTE" para a maioria e uma data para o
    resto, no mesmo campo. Quem lê a resposta via "14/09/2036" num campo chamado
    situação e tinha que adivinhar o que era.
    """
    if not situacao:
        return None
    try:
        return datetime.strptime(situacao.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


async def _classificar(
    conexao: Any,
    cliente: QwenClient | None,
    registro: dict[str, Any],
    *,
    modelo: str,
    usar_cache: bool,
    a_gravar: list[dict[str, Any]] | None = None,
    permitir_llm: bool = True,
) -> ClassificacaoIA:
    """Classificação de um registro: cache primeiro, LLM depois, erro claro por último."""
    numero = registro["numero_registro"]

    if usar_cache and conexao is not None:
        cacheado = classificacao_em_cache(conexao, numero)
        if cacheado is not None:
            return ClassificacaoIA(
                usa_ia=bool(cacheado["usa_ia"]),
                confianca=float(cacheado["confianca"]),
                justificativa=str(cacheado["justificativa"]),
                evidencia_confere=(
                    None
                    if cacheado.get("evidencia_confere") is None
                    else bool(cacheado["evidencia_confere"])
                ),
                origem="cache",
            )

    if cliente is None:
        if not permitir_llm:
            return ClassificacaoIA(
                usa_ia=None,
                justificativa=(
                    "Servidor em modo somente leitura: classifica apenas o que já está "
                    "em cache. Este registro ainda não foi avaliado."
                ),
                origem="nao_classificado",
            )
        return ClassificacaoIA(
            usa_ia=None,
            justificativa="LLM local indisponível: registro não classificado.",
            origem="indisponivel",
        )

    try:
        resultado = await classificar_dispositivo(
            cliente,
            nome=registro["nome_produto"],
            descricao=registro.get("descricao") or "",
        )
    except QwenIndisponivel as erro:
        logger.warning("classificação de %s falhou: %s", numero, erro)
        return ClassificacaoIA(
            usa_ia=None,
            justificativa=f"LLM local indisponível: {erro}",
            origem="indisponivel",
        )

    # O modelo aponta os trechos em que se baseou, e eles sao conferidos contra
    # o registro: sem isso, ele descreve o produto de memoria quando o texto e
    # vago, e a justificativa deixa de servir de controle.
    evidencia = verificar_evidencia(
        resultado.termos_citados,
        f"{registro['nome_produto']} {registro.get('descricao') or ''}",
    )
    if not evidencia.confere:
        logger.warning(
            "classificação de %s cita o que não está no registro: %s",
            numero,
            ", ".join(evidencia.ausentes),
        )

    if a_gravar is not None:
        a_gravar.append(
            {
                "numero_registro": numero,
                "usa_ia": resultado.usa_ia,
                "confianca": resultado.confianca,
                "justificativa": resultado.justificativa,
                "modelo": modelo,
                "evidencia_confere": evidencia.confere,
            }
        )

    return ClassificacaoIA(
        usa_ia=resultado.usa_ia,
        confianca=resultado.confianca,
        justificativa=resultado.justificativa,
        evidencia_confere=evidencia.confere,
        origem="llm",
    )


def _persistir_classificacoes(caminho_db: str, pendentes: list[dict[str, Any]]) -> None:
    """Grava o cache numa conexão de escrita própria.

    A consulta roda em modo leitura (para não brigar com o sync pelo lock), mas
    o cache precisa escrever. Se a base estiver travada, o cache é pulado: é
    otimização, não resposta.
    """
    if not pendentes:
        return
    try:
        with conectar(caminho_db) as conexao:
            for item in pendentes:
                gravar_classificacao(conexao, **item)
    except BaseIndisponivel as erro:
        logger.warning("cache não gravado (base ocupada): %s", erro)
    except Exception:  # noqa: BLE001 - cache é otimização, não pode derrubar a tool
        logger.exception("cache não gravado")


async def buscar_samd_recentes(
    dias: int = 90,
    apenas_com_ia: bool = True,
    apenas_software: bool = True,
    *,
    caminho_db: str,
    permitir_llm: bool = True,
    qwen_endpoint: str,
    qwen_model: str,
    timeout_segundos: float = 120.0,
    max_tentativas: int = 3,
    limite: int = 50,
) -> RespostaSaMD:
    """Dispositivos Classe III/IV registrados nos últimos ``dias``, com uso de IA classificado.

    ``apenas_software`` filtra por palavra-chave antes de gastar chamadas de LLM.
    Sem ele, os primeiros registros por data são quase sempre cânulas, parafusos e
    testes rápidos: no último ano, 13 de 1.832 registros Classe III/IV mencionam
    software. O filtro troca recall por custo, e o aviso da resposta diz isso.
    """
    if dias <= 0:
        return RespostaSaMD(
            dias=dias,
            apenas_com_ia=apenas_com_ia,
            fonte="duckdb",
            total=0,
            resultados=[],
            aviso="O parâmetro 'dias' precisa ser maior que zero.",
        )

    fonte: Fonte = "duckdb"
    motivo_mock = AVISO_MOCK
    resposta: RespostaSaMD | None = None
    pendentes: list[dict[str, Any]] = []
    try:
        with conectar(caminho_db, somente_leitura=True) as conexao:
            total_no_periodo = contar_dispositivos_no_periodo(
                conexao, dias=dias, apenas_software=apenas_software
            )
            registros = dispositivos_no_periodo(
                conexao, dias=dias, limite=limite, apenas_software=apenas_software
            )
            if registros:
                resposta, pendentes = await _montar_resposta(
                    conexao,
                    registros,
                    dias,
                    apenas_com_ia,
                    fonte,
                    permitir_llm=permitir_llm,
                    qwen_endpoint=qwen_endpoint,
                    qwen_model=qwen_model,
                    timeout_segundos=timeout_segundos,
                    max_tentativas=max_tentativas,
                    aviso=(
                        f"{AVISO_HEURISTICA} {AVISO_FILTRO_SOFTWARE}"
                        if apenas_software
                        else AVISO_HEURISTICA
                    ),
                    total_no_periodo=total_no_periodo,
                )
    except FileNotFoundError:
        pass
    except BaseIndisponivel as erro:
        logger.warning("base local indisponível: %s", erro)
        motivo_mock = AVISO_BASE_TRAVADA
    except Exception:  # noqa: BLE001 - nenhuma falha de base pode derrubar a tool
        logger.exception("falha ao consultar o DuckDB")
        motivo_mock = AVISO_BASE_TRAVADA

    if resposta is not None:
        # Fora do 'with': o DuckDB recusa uma conexão de escrita enquanto uma
        # de leitura ao mesmo arquivo está aberta no mesmo processo.
        _persistir_classificacoes(caminho_db, pendentes)
        return resposta

    mock, _ = await _montar_resposta(
        None,
        _MOCK,
        dias,
        apenas_com_ia,
        "mock",
        permitir_llm=permitir_llm,
        qwen_endpoint=qwen_endpoint,
        qwen_model=qwen_model,
        timeout_segundos=timeout_segundos,
        max_tentativas=max_tentativas,
        aviso=f"{motivo_mock} {AVISO_HEURISTICA}",
    )
    return mock


def _finalizar(
    itens: list[DispositivoSaMD],
    dias: int,
    apenas_com_ia: bool,
    fonte: Fonte,
    aviso: str,
    pendentes: list[dict[str, Any]] | None = None,
    total_no_periodo: int | None = None,
) -> tuple[RespostaSaMD, list[dict[str, Any]]]:
    """Filtra, conta os indeterminados e monta a resposta.

    Compartilhado pelos dois caminhos, com LLM e no modo connector, para que
    a contagem de indeterminados e os avisos sejam os mesmos nos dois.
    """
    analisados = len(itens)
    indeterminados = 0
    if apenas_com_ia:
        # Um "não" com confiança baixa quer dizer "o texto não deixa saber". Sumir
        # com esses em silêncio esconderia justamente os casos duvidosos, então
        # eles saem da lista mas entram na contagem. Vale também para o que nem
        # chegou a ser classificado.
        indeterminados = sum(
            1
            for item in itens
            if not item.classificacao.usa_ia
            and (
                item.classificacao.origem in ("nao_classificado", "indisponivel")
                or (item.classificacao.confianca or 0.0) < LIMIAR_INDETERMINADO
            )
        )
        itens = [item for item in itens if item.classificacao.usa_ia]

    if any(not item.visto_na_ultima_coleta for item in itens):
        aviso = f"{aviso} {AVISO_AUSENTE_NA_FONTE}"

    total = total_no_periodo if total_no_periodo is not None else analisados
    truncado = total > analisados
    if truncado:
        aviso = (
            f"{aviso} Havia {total} registros Classe III/IV no período e esta chamada "
            f"analisou os {analisados} mais recentes. Os demais não foram olhados, "
            f"então não conte os resultados para dizer quantos foram registrados: "
            f"aumente 'limite' ou reduza 'dias'."
        )

    return (
        RespostaSaMD(
            dias=dias,
            apenas_com_ia=apenas_com_ia,
            fonte=fonte,
            total=total,
            analisados=analisados,
            retornados=len(itens),
            truncado=truncado,
            resultados=itens,
            indeterminados=indeterminados,
            aviso=(
                f"{aviso} {indeterminados} registro(s) ficaram de fora por confiança "
                "abaixo de 0,5 ou por não terem sido classificados: isso não "
                "significa que não usem IA."
                if indeterminados
                else aviso
            ),
        ),
        pendentes or [],
    )


async def _montar_resposta(
    conexao: Any,
    registros: list[dict[str, Any]],
    dias: int,
    apenas_com_ia: bool,
    fonte: Fonte,
    *,
    total_no_periodo: int | None = None,
    permitir_llm: bool = True,
    qwen_endpoint: str,
    qwen_model: str,
    timeout_segundos: float,
    max_tentativas: int,
    aviso: str,
) -> tuple[RespostaSaMD, list[dict[str, Any]]]:
    pendentes: list[dict[str, Any]] = []
    if not permitir_llm:
        # Modo connector: nenhuma chamada ao LLM e nenhuma escrita na base. Quem
        # hospeda nao paga inferencia por todo mundo, e o DuckDB nao aceita
        # escritor enquanto o servidor le.
        itens = [
            DispositivoSaMD(
                numero_registro=registro["numero_registro"],
                nome_produto=registro["nome_produto"],
                empresa_detentora=registro.get("empresa_detentora"),
                classe_risco=registro.get("classe_risco"),
                situacao=registro.get("situacao"),
                validade_registro=_validade(registro.get("situacao")),
                data_registro=registro.get("data_registro"),
                visto_na_ultima_coleta=bool(registro.get("visto_na_ultima_coleta", True)),
                classificacao=await _classificar(
                    conexao,
                    None,
                    registro,
                    modelo=qwen_model,
                    usar_cache=fonte == "duckdb",
                    permitir_llm=False,
                ),
            )
            for registro in registros
        ]
        return _finalizar(
            itens,
            dias,
            apenas_com_ia,
            fonte,
            f"{aviso} {AVISO_SEM_LLM}",
            total_no_periodo=total_no_periodo,
        )

    async with QwenClient(
        qwen_endpoint,
        qwen_model,
        timeout_segundos=timeout_segundos,
        max_tentativas=max_tentativas,
    ) as cliente:
        vivo = await cliente.esta_vivo()
        ativo = cliente if vivo else None
        itens = [
            DispositivoSaMD(
                numero_registro=registro["numero_registro"],
                nome_produto=registro["nome_produto"],
                empresa_detentora=registro.get("empresa_detentora"),
                classe_risco=registro.get("classe_risco"),
                situacao=registro.get("situacao"),
                validade_registro=_validade(registro.get("situacao")),
                data_registro=registro.get("data_registro"),
                visto_na_ultima_coleta=bool(registro.get("visto_na_ultima_coleta", True)),
                classificacao=await _classificar(
                    conexao,
                    ativo,
                    registro,
                    modelo=qwen_model,
                    usar_cache=fonte == "duckdb",
                    a_gravar=pendentes if fonte == "duckdb" and permitir_llm else None,
                    permitir_llm=permitir_llm,
                ),
            )
            for registro in registros
        ]

    return _finalizar(
        itens, dias, apenas_com_ia, fonte, aviso, pendentes, total_no_periodo=total_no_periodo
    )
