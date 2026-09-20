"""Tool ``buscar_samd_recentes``.

Dispositivos médicos Classe III/IV registrados numa janela de tempo, classificados
quanto a uso de IA pela camada LLM. A classificação é heurística e vai marcada
como tal em cada item: nunca apresentar como fato regulatório.

Independente da tool de medicamentos.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from anvisa_mcp.llm.classify_samd import classificar_dispositivo
from anvisa_mcp.llm.qwen_client import QwenClient, QwenIndisponivel
from anvisa_mcp.store.db import conectar
from anvisa_mcp.store.queries import (
    classificacao_em_cache,
    dispositivos_no_periodo,
    gravar_classificacao,
)

logger = logging.getLogger(__name__)

Fonte = Literal["duckdb", "mock"]
OrigemClassificacao = Literal["llm", "cache", "indisponivel"]


class ClassificacaoIA(BaseModel):
    """Veredito heurístico sobre uso de IA — leia junto com a justificativa."""

    usa_ia: bool | None = Field(description="None quando não foi possível classificar")
    confianca: float | None = Field(default=None, ge=0.0, le=1.0)
    justificativa: str
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
    situacao: str | None = None
    data_registro: date | None = None
    classificacao: ClassificacaoIA


class RespostaSaMD(BaseModel):
    """Retorno estruturado da tool."""

    dias: int
    apenas_com_ia: bool
    fonte: Fonte = Field(
        description="'mock' = dado de exemplo, ainda não é registro real da Anvisa"
    )
    total: int
    resultados: list[DispositivoSaMD]
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

AVISO_MOCK = (
    "Dados de exemplo: a base local ainda não foi sincronizada com a Anvisa. "
    "Não use como informação regulatória."
)
AVISO_HEURISTICA = (
    "A Anvisa não publica campo estruturado de uso de IA. A classificação abaixo é "
    "heurística, feita por LLM lendo o texto do registro: confira a justificativa."
)


async def _classificar(
    conexao: Any,
    cliente: QwenClient | None,
    registro: dict[str, Any],
    *,
    modelo: str,
    usar_cache: bool,
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
                origem="cache",
            )

    if cliente is None:
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

    if conexao is not None:
        try:
            gravar_classificacao(
                conexao,
                numero_registro=numero,
                usa_ia=resultado.usa_ia,
                confianca=resultado.confianca,
                justificativa=resultado.justificativa,
                modelo=modelo,
            )
        except Exception:  # noqa: BLE001 - cache é otimização, não pode derrubar a tool
            logger.exception("não consegui gravar a classificação de %s", numero)

    return ClassificacaoIA(
        usa_ia=resultado.usa_ia,
        confianca=resultado.confianca,
        justificativa=resultado.justificativa,
        origem="llm",
    )


async def buscar_samd_recentes(
    dias: int = 90,
    apenas_com_ia: bool = True,
    *,
    caminho_db: str,
    qwen_endpoint: str,
    qwen_model: str,
    timeout_segundos: float = 120.0,
    max_tentativas: int = 3,
    limite: int = 50,
) -> RespostaSaMD:
    """Dispositivos Classe III/IV registrados nos últimos ``dias``, com uso de IA classificado."""
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
    try:
        with conectar(caminho_db) as conexao:
            registros = dispositivos_no_periodo(conexao, dias=dias, limite=limite)
            if registros:
                return await _montar_resposta(
                    conexao,
                    registros,
                    dias,
                    apenas_com_ia,
                    fonte,
                    qwen_endpoint=qwen_endpoint,
                    qwen_model=qwen_model,
                    timeout_segundos=timeout_segundos,
                    max_tentativas=max_tentativas,
                    aviso=AVISO_HEURISTICA,
                )
    except Exception:  # noqa: BLE001 - base indisponível não pode derrubar a tool
        logger.exception("falha ao consultar o DuckDB; caindo para mock")

    return await _montar_resposta(
        None,
        _MOCK,
        dias,
        apenas_com_ia,
        "mock",
        qwen_endpoint=qwen_endpoint,
        qwen_model=qwen_model,
        timeout_segundos=timeout_segundos,
        max_tentativas=max_tentativas,
        aviso=f"{AVISO_MOCK} {AVISO_HEURISTICA}",
    )


async def _montar_resposta(
    conexao: Any,
    registros: list[dict[str, Any]],
    dias: int,
    apenas_com_ia: bool,
    fonte: Fonte,
    *,
    qwen_endpoint: str,
    qwen_model: str,
    timeout_segundos: float,
    max_tentativas: int,
    aviso: str,
) -> RespostaSaMD:
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
                data_registro=registro.get("data_registro"),
                classificacao=await _classificar(
                    conexao, ativo, registro, modelo=qwen_model, usar_cache=fonte == "duckdb"
                ),
            )
            for registro in registros
        ]

    if apenas_com_ia:
        itens = [item for item in itens if item.classificacao.usa_ia]

    return RespostaSaMD(
        dias=dias,
        apenas_com_ia=apenas_com_ia,
        fonte=fonte,
        total=len(itens),
        resultados=itens,
        aviso=aviso,
    )
