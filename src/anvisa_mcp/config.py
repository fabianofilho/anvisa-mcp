"""Configuração do projeto, lida do ambiente (.env)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Valores lidos de .env, com defaults que batem com esta máquina."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    qwen_endpoint: str = Field(default="http://127.0.0.1:8080/v1")
    qwen_model: str = Field(default="local-model")
    qwen_timeout_segundos: float = Field(default=120.0)
    qwen_max_tentativas: int = Field(default=3, ge=1)

    duckdb_path: Path = Field(default=Path("./data/anvisa.duckdb"))
    # Horário fixo, não intervalo: a GPU serializa as chamadas ao Qwen, então os
    # syncs dos projetos são escalonados de madrugada para não competirem entre si
    # nem com uso interativo. Este projeto fica às 03:20.
    sync_hora_local: str = Field(default="03:20", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    log_level: str = Field(default="INFO")

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

    @property
    def modo_connector(self) -> bool:
        """No modo connector o servidor nao chama o LLM nem escreve na base.

        Classificar sob demanda exigiria inferencia paga por quem hospeda e
        escrita no arquivo que o proprio servidor le — e o DuckDB recusa abrir
        para escrita com um leitor aberto. As classificacoes vem do cache, que
        e construido na coleta, fora do servidor.
        """
        return self.transporte == "streamable-http"


def carregar_config() -> Config:
    """Config a partir do ambiente. Chamada na borda, nunca no import de módulo."""
    return Config()
