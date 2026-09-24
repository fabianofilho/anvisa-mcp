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


# Teto de registros avaliados por rodada de classificacao. Com filtro de software,
# 10 anos de registros Classe III/IV dao cerca de 310 candidatos.
LIMITE_CLASSIFICACAO = 5000


async def _classificar_pendentes(
    caminho_db: str, config: Config, *, dias: int, reclassificar: bool = False
) -> int:
    """Passa o LLM nos dispositivos do período que ainda não têm veredito.

    Roda pela própria tool, em vez de duplicar a heurística: ela já sabe filtrar
    o que parece software, reaproveitar o cache e gravar o resultado. Aqui só
    interessa o efeito colateral de encher o cache, então o retorno é quantos
    registros passaram pelo LLM nesta rodada.

    Com ``reclassificar``, vereditos antigos sem checagem de evidência
    (``evidencia_confere`` nulo) também voltam ao LLM. Depois de reclassificados
    eles ganham o campo, então a opção pode ficar ligada no timer: cada veredito
    antigo é refeito uma vez só.
    """
    resposta = await buscar_samd_recentes(
        dias=dias,
        apenas_com_ia=False,
        caminho_db=caminho_db,
        permitir_llm=True,
        gravar_cache=True,
        reclassificar_sem_evidencia=reclassificar,
        qwen_endpoint=config.qwen_endpoint,
        qwen_model=config.qwen_model,
        timeout_segundos=config.qwen_timeout_segundos,
        max_tentativas=config.qwen_max_tentativas,
        limite=LIMITE_CLASSIFICACAO,
    )
    if resposta.fonte != "duckdb":
        # Sem base, a tool devolve o exemplo; classifica-lo nao grava nada.
        typer.secho(
            f"nada classificado: a base em {caminho_db} não existe ou está vazia. "
            "Rode 'anvisa-cli sync' antes.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if resposta.truncado:
        typer.secho(
            f"janela com {resposta.total} candidatos, avaliados os {resposta.analisados} "
            "mais recentes",
            fg=typer.colors.YELLOW,
        )
    return sum(1 for item in resposta.resultados if item.classificacao.origem == "llm")


def _publicar_ou_sair(config: Config, *, forcar: bool) -> None:
    """Troca a base em construção pela servida, ou sai com código 1 se suspeita."""
    try:
        publicado = publicar(config.duckdb_path, forcar=forcar)
    except BaseSuspeita as erro:
        typer.secho(f"publicação recusada: {erro}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from erro
    typer.secho(f"base publicada: {publicado}", fg=typer.colors.GREEN)


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
    reclassificar: bool = typer.Option(
        False,
        "--reclassificar",
        help="Com --classificar, refaz também os vereditos antigos sem checagem de evidência",
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
            classificados = await _classificar_pendentes(
                str(alvo), config, dias=dias_classificacao, reclassificar=reclassificar
            )
            typer.echo(f"classificados nesta coleta: {classificados}")

        if publicar_ao_fim:
            _publicar_ou_sair(config, forcar=forcar)

    asyncio.run(rodar())


@app.command()
def classificar(
    dias: int = typer.Option(365, help="Janela, em dias, dos dispositivos a avaliar"),
    publicar_ao_fim: bool = typer.Option(
        False,
        "--publicar",
        help="Classifica numa cópia da base e troca por rename no fim (modo connector)",
    ),
    reclassificar: bool = typer.Option(
        False,
        "--reclassificar",
        help="Refaz também os vereditos antigos sem checagem de evidência",
    ),
    forcar: bool = typer.Option(False, "--forcar", help="Publica mesmo se a base nova encolheu"),
) -> None:
    """Passa o LLM nos dispositivos ainda sem veredito de uso de IA.

    Serve para encher o cache sem refazer a coleta. Quem já tem veredito não é
    reavaliado, exceto com ``--reclassificar``.

    Com ``--publicar`` segue o mesmo caminho do sync: clona a base servida,
    grava na cópia e troca no fim. Use sempre que houver um servidor lendo o
    arquivo, porque escrever direto nele disputa o lock com as consultas.
    """
    config = carregar_config()
    _configurar_log(config.log_level)
    alvo = clonar_para_construcao(config.duckdb_path) if publicar_ao_fim else config.duckdb_path

    async def rodar() -> None:
        novos = await _classificar_pendentes(
            str(alvo), config, dias=dias, reclassificar=reclassificar
        )
        typer.secho(f"classificados agora: {novos}", fg=typer.colors.GREEN)
        if publicar_ao_fim:
            _publicar_ou_sair(config, forcar=forcar)

    asyncio.run(rodar())


@app.command()
def medicamento(termo: str) -> None:
    """Testa a tool de medicamentos fora do MCP (nome, princípio ativo ou registro)."""
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
