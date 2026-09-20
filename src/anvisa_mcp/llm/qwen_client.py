"""Cliente do endpoint OpenAI-compatible exposto pelo ODS (Qwen local).

Assíncrono, com timeout e retry com backoff exponencial. Quando o ODS está
fora do ar, levanta ``QwenIndisponivel`` — nunca trava o servidor MCP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class QwenIndisponivel(RuntimeError):
    """O LLM local não respondeu, ou respondeu algo que não dá para usar."""


class ClassificationResult(BaseModel):
    """Saída da classificação heurística de uso de IA num dispositivo médico."""

    usa_ia: bool
    confianca: float = Field(ge=0.0, le=1.0)
    justificativa: str


_JSON_NA_RESPOSTA = re.compile(r"\{.*\}", re.DOTALL)


class QwenClient:
    """Cliente mínimo de chat completions.

    Usado como context manager assíncrono para que o httpx.AsyncClient feche::

        async with QwenClient(endpoint, modelo) as cliente:
            await cliente.classify(texto, template)
    """

    def __init__(
        self,
        endpoint: str,
        modelo: str,
        *,
        timeout_segundos: float = 120.0,
        max_tentativas: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._modelo = modelo
        self._timeout = timeout_segundos
        self._max_tentativas = max_tentativas
        self._client = client
        self._client_proprio = client is None

    async def __aenter__(self) -> QwenClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None and self._client_proprio:
            await self._client.aclose()
            self._client = None

    async def esta_vivo(self) -> bool:
        """True se o endpoint responde. Nunca levanta: serve para degradar com graça."""
        cliente = self._exigir_client()
        try:
            resposta = await cliente.get(f"{self._endpoint}/models", timeout=5.0)
            return resposta.status_code == 200
        except httpx.HTTPError as erro:
            logger.warning("LLM local não respondeu em %s: %s", self._endpoint, erro)
            return False

    async def chat(
        self,
        mensagens: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 600,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Uma chat completion, com retry e backoff exponencial."""
        cliente = self._exigir_client()
        payload: dict[str, Any] = {
            "model": self._modelo,
            "messages": mensagens,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        ultimo_erro: Exception | None = None
        for tentativa in range(1, self._max_tentativas + 1):
            try:
                resposta = await cliente.post(
                    f"{self._endpoint}/chat/completions",
                    json=payload,
                    timeout=self._timeout,
                )
                resposta.raise_for_status()
                corpo = resposta.json()
                return str(corpo["choices"][0]["message"]["content"])
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as erro:
                ultimo_erro = erro
                if tentativa == self._max_tentativas:
                    break
                espera = 2 ** (tentativa - 1)
                logger.warning(
                    "tentativa %d/%d falhou (%s); nova tentativa em %ds",
                    tentativa,
                    self._max_tentativas,
                    erro,
                    espera,
                )
                await asyncio.sleep(espera)

        raise QwenIndisponivel(
            f"LLM local em {self._endpoint} falhou após "
            f"{self._max_tentativas} tentativas: {ultimo_erro}"
        ) from ultimo_erro

    async def classify(self, text: str, prompt_template: str) -> ClassificationResult:
        """Classifica ``text`` usando ``prompt_template`` (Jinja2 já renderizado).

        O template manda o modelo responder só com JSON. Como modelo pequeno às
        vezes embrulha o JSON em prosa, a extração tolera isso antes de validar.
        """
        conteudo = await self.chat(
            [
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
        )
        return self._parse(conteudo)

    @staticmethod
    def _parse(conteudo: str) -> ClassificationResult:
        bruto = conteudo.strip()
        try:
            return ClassificationResult.model_validate_json(bruto)
        except ValidationError:
            pass

        achado = _JSON_NA_RESPOSTA.search(bruto)
        if achado is None:
            raise QwenIndisponivel(f"resposta do LLM não continha JSON: {bruto[:200]!r}")
        try:
            return ClassificationResult.model_validate(json.loads(achado.group(0)))
        except (ValidationError, json.JSONDecodeError) as erro:
            raise QwenIndisponivel(
                f"resposta do LLM não bate com o schema esperado: {bruto[:200]!r}"
            ) from erro

    def _exigir_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("QwenClient precisa ser usado como 'async with'")
        return self._client
