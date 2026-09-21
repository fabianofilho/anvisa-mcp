"""Publicação da base por troca atômica.

O DuckDB aceita um escritor por vez e recusa abrir para escrita enquanto houver
um leitor, verificado: com uma conexão read-only aberta, o escritor leva
``ConnectionException``. No modo connector, o servidor abre a base a cada
requisição, então uma coleta escrevendo direto no arquivo servido falharia toda
vez que caísse em cima de uma consulta.

A saída é não escrever no arquivo servido: a coleta constrói uma base nova ao
lado e, no fim, um ``os.replace`` troca as duas. No POSIX isso é atômico, quem
já abriu o arquivo antigo continua lendo o inode antigo até fechar (o que aqui
dura o tempo de uma requisição), e quem abrir depois pega o novo.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SUFIXO_EM_CONSTRUCAO = ".novo"
SUFIXO_ANTERIOR = ".anterior"


def caminho_em_construcao(destino: Path | str) -> Path:
    """Onde a coleta escreve enquanto trabalha.

    Fica no mesmo diretório de propósito: ``os.replace`` só é atômico dentro do
    mesmo sistema de arquivos.
    """
    destino = Path(destino)
    return destino.with_name(destino.name + SUFIXO_EM_CONSTRUCAO)


class BaseSuspeita(RuntimeError):
    """A base nova encolheu demais em relação à servida.

    Coleta interrompida por rede ruim, portal respondendo truncado ou teste com
    ``--max-paginas`` produzem uma base pequena e aparentemente válida. Publicar
    isso substitui a base boa em silêncio, e num connector isso chega a todo
    mundo que consulta.
    """


def _contar(caminho: Path, tabela: str) -> int:
    """Linhas da tabela, ou -1 quando o arquivo não dá para abrir."""
    import duckdb

    try:
        conexao = duckdb.connect(str(caminho), read_only=True)
    except duckdb.Error:
        return -1
    try:
        linha = conexao.execute(f"SELECT count(*) FROM {tabela}").fetchone()
        return int(linha[0]) if linha else 0
    except duckdb.Error:
        return -1
    finally:
        conexao.close()


def publicar(
    destino: Path | str,
    *,
    guardar_anterior: bool = True,
    tabela_referencia: str = "medicamentos",
    fracao_minima: float = 0.9,
    forcar: bool = False,
) -> Path:
    """Troca a base em construção pela servida, atomicamente.

    Args:
        destino: o arquivo que o servidor lê.
        guardar_anterior: mantém a versão trocada como ``.anterior``, para
            poder voltar atrás se a coleta nova vier ruim.
        tabela_referencia: tabela usada na checagem de tamanho.
        fracao_minima: a base nova precisa ter ao menos esta fração das linhas
            da servida. O padrão tolera remoção normal na fonte e barra queda
            abrupta.
        forcar: publica mesmo com a base menor. Para o primeiro carregamento ou
            quando a fonte realmente encolheu.

    Raises:
        FileNotFoundError: quando não há base em construção para publicar.
        BaseSuspeita: quando a base nova encolheu além do tolerado.
    """
    destino = Path(destino)
    origem = caminho_em_construcao(destino)
    if not origem.exists():
        raise FileNotFoundError(f"nada para publicar: {origem} não existe")

    if not forcar and destino.exists():
        atual = _contar(destino, tabela_referencia)
        nova = _contar(origem, tabela_referencia)
        if atual > 0 and nova >= 0 and nova < atual * fracao_minima:
            raise BaseSuspeita(
                f"a base nova tem {nova} linha(s) em {tabela_referencia} contra {atual} "
                f"da servida ({nova / atual:.0%}). Publicação recusada. Se a fonte "
                "realmente encolheu, publique com forcar=True."
            )

    if guardar_anterior and destino.exists():
        anterior = destino.with_name(destino.name + SUFIXO_ANTERIOR)
        os.replace(destino, anterior)
        logger.info("base anterior guardada em %s", anterior.name)

    os.replace(origem, destino)
    logger.info("base publicada: %s (%d bytes)", destino, destino.stat().st_size)

    # O WAL pertence ao arquivo antigo: deixado para trás, o DuckDB tentaria
    # aplicá-lo sobre a base nova.
    wal = origem.with_name(origem.name + ".wal")
    if wal.exists():
        wal.unlink()
    return destino


def reverter(destino: Path | str) -> Path:
    """Volta para a base anterior, se houver.

    Raises:
        FileNotFoundError: quando não há versão anterior guardada.
    """
    destino = Path(destino)
    anterior = destino.with_name(destino.name + SUFIXO_ANTERIOR)
    if not anterior.exists():
        raise FileNotFoundError(f"não há base anterior em {anterior}")
    os.replace(anterior, destino)
    logger.warning("base revertida para a versão anterior")
    return destino
