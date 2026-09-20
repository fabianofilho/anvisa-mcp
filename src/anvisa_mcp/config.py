"""Configuração do projeto, lida do ambiente (.env)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Valores lidos de .env, com defaults que batem com esta máquina."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    qwen_endpoint: str = Field(default="http://127.0.0.1:11434/v1")
    qwen_model: str = Field(default="local-model")
    qwen_timeout_segundos: float = Field(default=120.0)
    qwen_max_tentativas: int = Field(default=3, ge=1)

    duckdb_path: Path = Field(default=Path("./data/anvisa.duckdb"))
    # Horário fixo, não intervalo: a GPU serializa as chamadas ao Qwen, então os
    # syncs dos projetos são escalonados de madrugada para não competirem entre si
    # nem com uso interativo. Este projeto fica às 03:20.
    sync_hora_local: str = Field(default="03:20", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    log_level: str = Field(default="INFO")


def carregar_config() -> Config:
    """Config a partir do ambiente. Chamada na borda, nunca no import de módulo."""
    return Config()
