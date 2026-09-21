"""CLI de administração: sync manual e teste das tools fora do MCP."""

from __future__ import annotations

import asyncio
import json
import logging

import typer

from anvisa_mcp.config import Config, carregar_config
from anvisa_mcp.data.sources import FONTES, FonteNaoConfigurada
from anvisa_mcp.data.sync import sync_dispositivos_medicos, sync_medicamentos
from anvisa_mcp.llm.qwen_client import QwenClient
from anvisa_mcp.mcp_server.tools.medicamentos import consultar_status_medicamento
from anvisa_mcp.mcp_server.tools.samd import buscar_samd_recentes
from anvisa_mcp.store.db import conectar
from anvisa_mcp.store.queries import ausentes_na_ultima_coleta
from anvisa_mcp.store.troca import BaseSuspeita, clonar_para_construcao, publicar

app = typer.Typer(help="Administração do anvisa-mcp", no_args_is_help=True)


def _configurar_log(nivel: str) -> None:
    logging.basicConfig(
        level=getattr(logging, nivel.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )


async def _classificar_pendentes(caminho_db: str, config: Config, *, dias: int) -> int:
    """Passa o LLM nos dispositivos do período que ainda não têm veredito.

    Roda pela própria tool, em vez de duplicar a heurística: ela já sabe filtrar
    o que parece software, reaproveitar o cache e gravar o resultado. Aqui só
    interessa o efeito colateral de encher o cache, então o retorno é a contagem.
    """
    antes = _total_classificacoes(caminho_db)
    await buscar_samd_recentes(
        dias=dias,
        apenas_com_ia=False,
        caminho_db=caminho_db,
        permitir_llm=True,
        qwen_endpoint=config.qwen_endpoint,
        qwen_model=config.qwen_model,
        timeout_segundos=config.qwen_timeout_segundos,
        max_tentativas=config.qwen_max_tentativas,
        limite=500,
    )
    return _total_classificacoes(caminho_db) - antes


def _total_classificacoes(caminho_db: str) -> int:
    with conectar(caminho_db, somente_leitura=True) as conexao:
        linha = conexao.execute("SELECT count(*) FROM classificacoes_samd").fetchone()
    return int(linha[0]) if linha else 0


@app.command()
def fontes() -> None:
    """Mostra as fontes de dados e se a URL já foi confirmada."""
    for chave, fonte in FONTES.items():
        estado = fonte.url or "URL NÃO CONFIRMADA"
        typer.echo(f"{chave}: {estado}")
        typer.echo(f"  {fonte.descricao}")
        if not fonte.url:
            typer.echo(f"  como confirmar: {fonte.onde_encontrar}")


@app.command()
def sync(
    fonte: str = typer.Option("todas", help="medicamentos, dispositivos_medicos ou todas"),
    publicar_ao_fim: bool = typer.Option(
        False,
        "--publicar",
        help="Constrói a base ao lado e troca por rename no fim (modo connector)",
    ),
    forcar: bool = typer.Option(False, "--forcar", help="Publica mesmo se a base nova encolheu"),
    classificar_ao_fim: bool = typer.Option(
        False,
        "--classificar",
        help="Classifica os dispositivos novos com o LLM antes de publicar",
    ),
    dias_classificacao: int = typer.Option(
        365, help="Janela, em dias, dos dispositivos a classificar com --classificar"
    ),
) -> None:
    """Roda o sync dos datasets públicos agora.

    Com ``--publicar``, escreve numa base nova e só troca pela servida no fim.
    É o modo para quando há um servidor HTTP lendo o arquivo: o DuckDB recusa
    abrir para escrita enquanto houver leitor.

    ``--classificar`` roda o LLM sobre os dispositivos ainda sem veredito. No
    modo connector é o único momento em que o LLM entra: o servidor não
    classifica sob demanda, só serve o que já está no cache.
    """
    config = carregar_config()
    _configurar_log(config.log_level)

    alvo = clonar_para_construcao(config.duckdb_path) if publicar_ao_fim else config.duckdb_path

    async def rodar() -> None:
        with conectar(alvo) as conexao:
            tarefas = {
                "medicamentos": sync_medicamentos,
                "dispositivos_medicos": sync_dispositivos_medicos,
            }
            escolhidas = tarefas if fonte == "todas" else {fonte: tarefas[fonte]}
            for nome, tarefa in escolhidas.items():
                try:
                    resultado = await tarefa(conexao)
                    typer.echo(
                        f"{nome}: {resultado.novos} novos, {resultado.atualizados} atualizados"
                    )
                except FonteNaoConfigurada as erro:
                    typer.secho(f"{nome}: {erro}", fg=typer.colors.YELLOW)

        if classificar_ao_fim:
            classificados = await _classificar_pendentes(str(alvo), config, dias=dias_classificacao)
            typer.echo(f"classificados nesta coleta: {classificados}")

        if publicar_ao_fim:
            try:
                publicado = publicar(config.duckdb_path, forcar=forcar)
            except BaseSuspeita as erro:
                typer.secho(f"publicação recusada: {erro}", fg=typer.colors.RED)
                raise typer.Exit(code=1) from erro
            typer.secho(f"base publicada: {publicado}", fg=typer.colors.GREEN)

    asyncio.run(rodar())


@app.command()
def classificar(
    dias: int = typer.Option(365, help="Janela, em dias, dos dispositivos a avaliar"),
) -> None:
    """Passa o LLM nos dispositivos ainda sem veredito de uso de IA.

    Serve para encher o cache sem refazer a coleta. Num deploy de connector é
    isto que produz as classificações: o servidor HTTP não chama o LLM, só serve
    o que já está gravado. Quem já tem veredito não é reavaliado.
    """
    config = carregar_config()
    _configurar_log(config.log_level)
    novos = asyncio.run(_classificar_pendentes(str(config.duckdb_path), config, dias=dias))
    typer.secho(f"classificados agora: {novos}", fg=typer.colors.GREEN)


@app.command()
def medicamento(termo: str) -> None:
    """Testa a tool de medicamentos fora do MCP."""
    config = carregar_config()
    resposta = asyncio.run(consultar_status_medicamento(termo, caminho_db=str(config.duckdb_path)))
    typer.echo(resposta.model_dump_json(indent=2))


@app.command()
def samd(dias: int = 90, apenas_com_ia: bool = True, apenas_software: bool = True) -> None:
    """Testa a tool de SaMD fora do MCP."""
    config = carregar_config()
    resposta = asyncio.run(
        buscar_samd_recentes(
            dias=dias,
            apenas_com_ia=apenas_com_ia,
            apenas_software=apenas_software,
            caminho_db=str(config.duckdb_path),
            qwen_endpoint=config.qwen_endpoint,
            qwen_model=config.qwen_model,
            timeout_segundos=config.qwen_timeout_segundos,
            max_tentativas=config.qwen_max_tentativas,
        )
    )
    typer.echo(resposta.model_dump_json(indent=2))


@app.command()
def llm() -> None:
    """Verifica se o LLM local (ODS) está respondendo."""
    config = carregar_config()

    async def checar() -> None:
        async with QwenClient(config.qwen_endpoint, config.qwen_model) as cliente:
            if not await cliente.esta_vivo():
                typer.secho(f"LLM local fora do ar em {config.qwen_endpoint}", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            resposta = await cliente.chat(
                [{"role": "user", "content": "Responda apenas: pronto"}], max_tokens=20
            )
            typer.secho(f"LLM respondeu: {resposta.strip()}", fg=typer.colors.GREEN)
            typer.echo(f"endpoint: {config.qwen_endpoint}  modelo: {config.qwen_model}")

    asyncio.run(checar())


@app.command()
def schema() -> None:
    """Mostra as tabelas do DuckDB local."""
    config = carregar_config()
    with conectar(config.duckdb_path) as conexao:
        tabelas = conexao.execute("SHOW TABLES").fetchall()
        contagens: dict[str, int] = {}
        for (tabela,) in tabelas:
            linha = conexao.execute(f"SELECT count(*) FROM {tabela}").fetchone()
            contagens[tabela] = int(linha[0]) if linha else 0
        # Quem nao veio na ultima coleta continua na base: o upsert nao remove.
        for tabela in ("medicamentos", "dispositivos_medicos"):
            if tabela in contagens:
                contagens[f"{tabela}_ausentes_na_ultima_coleta"] = ausentes_na_ultima_coleta(
                    conexao, tabela
                )
    typer.echo(json.dumps(contagens, indent=2))


if __name__ == "__main__":
    app()
