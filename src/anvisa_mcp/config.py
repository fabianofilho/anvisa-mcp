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
    sync_frequencia_horas: int = Field(default=24, ge=1)
    log_level: str = Field(default="INFO")


def carregar_config() -> Config:
    """Config a partir do ambiente. Chamada na borda, nunca no import de módulo."""
    return Config()
