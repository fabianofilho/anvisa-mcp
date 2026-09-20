"""Download e atualização dos datasets públicos da Anvisa.

Cada sync é idempotente: o mesmo arquivo baixado duas vezes não duplica linhas,
porque a gravação é um upsert pela chave ``numero_registro``.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any

import duckdb
import httpx

from anvisa_mcp.data.sources import DISPOSITIVOS_MEDICOS, MEDICAMENTOS, FonteDados

logger = logging.getLogger(__name__)

TIMEOUT_DOWNLOAD = 300.0


@dataclass(frozen=True)
class ResultadoSync:
    """Quantos registros entraram e quantos já existiam."""

    fonte: str
    novos: int
    atualizados: int

    @property
    def total(self) -> int:
        return self.novos + self.atualizados


async def _baixar_csv(url: str, *, client: httpx.AsyncClient | None = None) -> list[dict[str, str]]:
    """Baixa um CSV e devolve as linhas como dicionários."""
    proprio = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_DOWNLOAD, follow_redirects=True)
    try:
        resposta = await http.get(url)
        resposta.raise_for_status()
        texto = resposta.text
    finally:
        if proprio:
            await http.aclose()

    amostra = texto[:4096]
    try:
        dialeto = csv.Sniffer().sniff(amostra, delimiters=";,\t")
        delimitador = dialeto.delimiter
    except csv.Error:
        delimitador = ";"
    return list(csv.DictReader(io.StringIO(texto), delimiter=delimitador))


def _gravar(
    conexao: duckdb.DuckDBPyConnection,
    tabela: str,
    registros: list[dict[str, Any]],
) -> ResultadoSync:
    """Upsert dos registros na tabela, contando novos vs. atualizados."""
    if not registros:
        return ResultadoSync(fonte=tabela, novos=0, atualizados=0)

    colunas = list(registros[0].keys())
    marcadores = ", ".join("?" for _ in colunas)
    atribuicoes = ", ".join(f"{c} = excluded.{c}" for c in colunas if c != "numero_registro")

    existentes = {
        linha[0] for linha in conexao.execute(f"SELECT numero_registro FROM {tabela}").fetchall()
    }
    novos = sum(1 for r in registros if r["numero_registro"] not in existentes)

    conexao.executemany(
        f"""
        INSERT INTO {tabela} ({", ".join(colunas)})
        VALUES ({marcadores})
        ON CONFLICT (numero_registro) DO UPDATE SET {atribuicoes}, atualizado_em = now()
        """,
        [[r[c] for c in colunas] for r in registros],
    )
    return ResultadoSync(fonte=tabela, novos=novos, atualizados=len(registros) - novos)


async def _sync_fonte(
    conexao: duckdb.DuckDBPyConnection,
    fonte: FonteDados,
    *,
    client: httpx.AsyncClient | None = None,
) -> ResultadoSync:
    url = fonte.exigir_url()  # levanta FonteNaoConfigurada se a URL não foi confirmada
    logger.info("sincronizando %s de %s", fonte.chave, url)
    linhas = await _baixar_csv(url, client=client)
    registros = fonte.parser(linhas)
    resultado = _gravar(conexao, fonte.chave, registros)
    logger.info(
        "%s: %d novos, %d atualizados (%d linhas no arquivo)",
        fonte.chave,
        resultado.novos,
        resultado.atualizados,
        len(linhas),
    )
    return resultado


async def sync_medicamentos(
    conexao: duckdb.DuckDBPyConnection,
    *,
    client: httpx.AsyncClient | None = None,
) -> ResultadoSync:
    """Atualiza a tabela de medicamentos."""
    return await _sync_fonte(conexao, MEDICAMENTOS, client=client)


async def sync_dispositivos_medicos(
    conexao: duckdb.DuckDBPyConnection,
    *,
    client: httpx.AsyncClient | None = None,
) -> ResultadoSync:
    """Atualiza a tabela de dispositivos médicos."""
    return await _sync_fonte(conexao, DISPOSITIVOS_MEDICOS, client=client)


def agendar_syncs(caminho_db: str, frequencia_horas: int) -> Any:
    """Agenda os dois syncs no APScheduler e devolve o scheduler já iniciado."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from anvisa_mcp.store.db import conectar

    async def tarefa() -> None:
        with conectar(caminho_db) as conexao:
            for sync in (sync_medicamentos, sync_dispositivos_medicos):
                try:
                    await sync(conexao)
                except Exception:  # noqa: BLE001 - o agendador não pode morrer por uma fonte
                    logger.exception("sync falhou")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(tarefa, "interval", hours=frequencia_horas, next_run_time=None)
    scheduler.start()
    logger.info("syncs agendados a cada %dh", frequencia_horas)
    return scheduler
