"""Configuração do projeto, lida do ambiente (.env)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from anvisa_mcp.llm.jev_client import ParametrosJev


class ChaveJevAusente(RuntimeError):
    """A flag do Jev está ligada e nenhuma chave foi encontrada."""


def _ler_chave_do_arquivo(caminho: Path) -> str | None:
    """Lê TYPESAFE_API_KEY=... de um arquivo de ambiente fora do repo. None se não houver."""
    arquivo = caminho.expanduser()
    if not arquivo.exists():
        return None
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if linha.startswith("TYPESAFE_API_KEY="):
            valor = linha.split("=", 1)[1].strip()
            return valor or None
    return None


class Config(BaseSettings):
    """Valores lidos de .env, com defaults que batem com esta máquina."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    qwen_endpoint: str = Field(default="http://127.0.0.1:8080/v1")
    qwen_model: str = Field(default="local-model")
    qwen_timeout_segundos: float = Field(default=120.0)
    qwen_max_tentativas: int = Field(default=3, ge=1)

    duckdb_path: Path = Field(default=Path("./data/anvisa.duckdb"))
    log_level: str = Field(default="INFO")

    # --- classificador de SaMD: Qwen local (padrão) ou Jev (TypeSafe, nuvem) ---
    # Com "jev", só os três campos públicos da Anvisa (nome técnico, nome comercial
    # e fabricante) saem da máquina; o Qwen continua como fallback quando a nuvem
    # falha. Trocar a flag não refaz o cache: vereditos antigos continuam valendo.
    classificador_samd: Literal["qwen", "jev"] = Field(default="qwen")
    jev_endpoint: str = Field(default="https://api.typesafe.ai/v1/systemone")
    jev_model: str = Field(default="jev-1.13.0")
    jev_timeout_segundos: float = Field(default=30.0)
    # A chave vem de TYPESAFE_API_KEY no ambiente ou deste arquivo (linha
    # TYPESAFE_API_KEY=...), fora de qualquer repo. Nunca do .env do projeto.
    jev_arquivo_chave: Path = Field(default=Path("~/.config/jev-mcp/env"))
    typesafe_api_key: str | None = Field(default=None)

    def parametros_jev(self) -> ParametrosJev | None:
        """Parâmetros do Jev quando a flag está ligada; None mantém o comportamento antigo.

        Levanta ``ChaveJevAusente`` se a flag está ligada e não há chave: falhar
        alto na borda é melhor do que classificar em silêncio com o Qwen achando
        que foi o Jev.
        """
        if self.classificador_samd != "jev":
            return None
        chave = self.typesafe_api_key or _ler_chave_do_arquivo(self.jev_arquivo_chave)
        if not chave:
            raise ChaveJevAusente(
                "CLASSIFICADOR_SAMD=jev sem chave: defina TYPESAFE_API_KEY ou grave "
                f"TYPESAFE_API_KEY=... em {self.jev_arquivo_chave}"
            )
        return ParametrosJev(
            chave=chave,
            endpoint=self.jev_endpoint,
            modelo=self.jev_model,
            timeout_segundos=self.jev_timeout_segundos,
        )

    # --- modo connector (servidor HTTP publico) ---
    transporte: Literal["stdio", "streamable-http"] = Field(default="stdio")
    http_host: str = Field(default="127.0.0.1")
    http_porta: int = Field(default=8000, ge=1, le=65535)
    http_path: str = Field(default="/mcp")
    http_stateless: bool = Field(default=True)
    # O teto global protege a maquina: o trafego do Claude chega dos IPs da
    # Anthropic, entao limitar so por IP juntaria todos os usuarios num balde.
    http_limite_global_por_minuto: int = Field(default=1200, ge=1)
    http_limite_por_minuto: int = Field(default=600, ge=1)
    # Nomes pelos quais o servidor aceita ser chamado, separados por virgula.
    # Vazio significa so loopback. O SDK valida o cabecalho Host contra esta
    # lista e responde 421 fora dela: e defesa contra DNS rebinding, onde um
    # site qualquer faz o navegador da vitima falar com um servidor local. Atras
    # de um proxy ou tunel, o Host que chega e o nome publico, entao ele precisa
    # constar aqui; a alternativa seria desligar a checagem, que e pior.
    http_hosts_publicos: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("http_hosts_publicos", mode="before")
    @classmethod
    def _dividir_hosts(cls, valor: object) -> object:
        """Aceita "a.exemplo,b.exemplo" do .env, nao so lista JSON."""
        if isinstance(valor, str):
            return [p.strip() for p in valor.split(",") if p.strip()]
        return valor

    @property
    def modo_connector(self) -> bool:
        """No modo connector o servidor nao chama o LLM nem escreve na base.

        Classificar sob demanda exigiria inferencia paga por quem hospeda e
        escrita no arquivo que o proprio servidor le, e o DuckDB recusa abrir
        para escrita com um leitor aberto. As classificacoes vem do cache, que
        e construido na coleta, fora do servidor.
        """
        return self.transporte == "streamable-http"


def carregar_config() -> Config:
    """Config a partir do ambiente. Chamada na borda, nunca no import de módulo."""
    return Config()
