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
from anvisa_mcp.store.queries import ausentes_na_ultima_coleta

logger = logging.getLogger(__name__)

# Espalha o disparo dentro de meia hora. Num projeto publico isso nao e
# detalhe: horario fixo resolve a concorrencia na maquina de quem roda, mas
# cria concorrencia do outro lado se varias pessoas usarem o padrao do
# .env.example e baterem no mesmo servidor no mesmo minuto.
JITTER_SEGUNDOS = 1800

TIMEOUT_DOWNLOAD = 300.0

# Identifica o projeto para quem administra o portal de dados abertos, com link
# para o repositorio. Antes daqui o coletor usava o User-Agent default do httpx,
# anonimo — ma cidadania para um projeto publico que consulta servidor do governo.
USER_AGENT = "anvisa-mcp/0.1 (+https://github.com/fabianofilho/anvisa-mcp)"


@dataclass(frozen=True)
class ResultadoSync:
    """Quantos registros entraram e quantos já existiam."""

    fonte: str
    novos: int
    atualizados: int

    @property
    def total(self) -> int:
        return self.novos + self.atualizados


async def _baixar_csv(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    encoding: str = "iso-8859-1",
    delimitador: str = ";",
) -> list[dict[str, str]]:
    """Baixa um CSV e devolve as linhas como dicionários.

    A Anvisa publica em ISO-8859-1 sem declarar charset no cabeçalho HTTP, então
    a decodificação é explícita: deixar o httpx adivinhar produz acento quebrado.
    """
    proprio = client is None
    http = client or httpx.AsyncClient(
        timeout=TIMEOUT_DOWNLOAD, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        resposta = await http.get(url)
        resposta.raise_for_status()
        texto = resposta.content.decode(encoding, errors="replace")
    finally:
        if proprio:
            await http.aclose()

    return list(csv.DictReader(io.StringIO(texto), delimiter=delimitador))


def _gravar(
    conexao: duckdb.DuckDBPyConnection,
    tabela: str,
    registros: list[dict[str, Any]],
) -> ResultadoSync:
    """Upsert dos registros na tabela, contando novos vs. atualizados."""
    if not registros:
        return ResultadoSync(fonte=tabela, novos=0, atualizados=0)

    # O mesmo número de registro aparece em mais de uma linha (apresentações,
    # modelos). DuckDB recusa atualizar a mesma linha duas vezes no mesmo
    # comando, então fica a última ocorrência de cada chave.
    unicos: dict[str, dict[str, Any]] = {r["numero_registro"]: r for r in registros}
    registros = list(unicos.values())

    colunas = list(registros[0].keys())
    marcadores = ", ".join("?" for _ in colunas)
    atribuicoes = ", ".join(f"{c} = excluded.{c}" for c in colunas if c != "numero_registro")
    lista_colunas = ", ".join(colunas)

    # Um upsert por linha custa uma busca de índice por linha, e estes datasets
    # têm centenas de milhares delas. Em vez disso: carrega tudo numa tabela
    # temporária sem restrições e faz UM upsert em massa a partir dela.
    staging = f"staging_{tabela}"
    conexao.execute(
        f"CREATE OR REPLACE TEMP TABLE {staging} AS SELECT {lista_colunas} FROM {tabela} LIMIT 0"
    )
    conexao.executemany(
        f"INSERT INTO {staging} ({lista_colunas}) VALUES ({marcadores})",
        [[r[c] for c in colunas] for r in registros],
    )

    contagem = conexao.execute(
        f"""
        SELECT count(*) FROM {staging} s
        WHERE NOT EXISTS (SELECT 1 FROM {tabela} t WHERE t.numero_registro = s.numero_registro)
        """
    ).fetchone()
    novos = int(contagem[0]) if contagem else 0

    conexao.execute(
        f"""
        INSERT INTO {tabela} ({lista_colunas})
        SELECT {lista_colunas} FROM {staging}
        ON CONFLICT (numero_registro) DO UPDATE SET {atribuicoes}, atualizado_em = now()
        """
    )
    conexao.execute(f"DROP TABLE {staging}")
    return ResultadoSync(fonte=tabela, novos=novos, atualizados=len(registros) - novos)


async def _sync_fonte(
    conexao: duckdb.DuckDBPyConnection,
    fonte: FonteDados,
    *,
    client: httpx.AsyncClient | None = None,
) -> ResultadoSync:
    url = fonte.exigir_url()  # levanta FonteNaoConfigurada se a URL não foi confirmada
    logger.info("sincronizando %s de %s", fonte.chave, url)
    linhas = await _baixar_csv(
        url, client=client, encoding=fonte.encoding, delimitador=fonte.delimitador
    )
    registros = fonte.parser(linhas)
    resultado = _gravar(conexao, fonte.chave, registros)
    ausentes = ausentes_na_ultima_coleta(conexao, fonte.chave)
    if ausentes:
        # Nao e erro: e a Anvisa tendo removido registros da publicacao. Mas a
        # base guarda o ultimo estado visto, entao precisa ficar no log.
        logger.warning(
            "%s: %d registro(s) da base nao vieram neste arquivo; "
            "seguem marcados com visto_na_ultima_coleta=false",
            fonte.chave,
            ausentes,
        )
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


def agendar_syncs(caminho_db: str, hora_local: str) -> Any:
    """Agenda os dois syncs num horário fixo e devolve o scheduler já iniciado.

    ``hora_local`` é ``HH:MM``. É horário fixo, e não intervalo relativo, porque a
    GPU serializa as chamadas ao LLM: os syncs dos projetos locais são escalonados
    de madrugada para não competirem entre si nem com uso interativo.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from anvisa_mcp.store.db import conectar

    async def tarefa() -> None:
        with conectar(caminho_db) as conexao:
            for sync in (sync_medicamentos, sync_dispositivos_medicos):
                try:
                    await sync(conexao)
                except Exception:  # noqa: BLE001 - o agendador não pode morrer por uma fonte
                    logger.exception("sync falhou")

    hora, minuto = (int(parte) for parte in hora_local.split(":"))
    scheduler = AsyncIOScheduler()
    # jitter: ver JITTER_SEGUNDOS no topo do modulo
    scheduler.add_job(tarefa, "cron", hour=hora, minute=minuto, jitter=JITTER_SEGUNDOS)
    scheduler.start()
    logger.info("syncs agendados diariamente às %s", hora_local)
    return scheduler
