"""Classificação de uso de IA pelo Jev (TypeSafe), um modelo de decisão na nuvem.

O Jev não gera prosa: recebe um ``state`` e perguntas fechadas e devolve
probabilidades calibradas. Aqui ele responde uma pergunta sim/não ("o texto do
registro declara ou sugere IA?") e o código converte a probabilidade no mesmo
``ClassificationResult`` que o Qwen produz, para que o resto da tool não mude.

Privacidade: só os três campos públicos da Anvisa (nome técnico, nome comercial
e fabricante) saem da máquina. Nada mais deste projeto chama a nuvem.

Avaliação que motivou isto: avaliacoes-modelos, relatórios 2026-10-11*-jev-vs-qwen-samd.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from anvisa_mcp.llm.qwen_client import ClassificationResult, QwenClient, QwenIndisponivel

logger = logging.getLogger(__name__)

ENDPOINT_PADRAO = "https://api.typesafe.ai/v1/systemone"
# Pinado por versão: o alias "jev-latest" pode passar a apontar para outro modelo
# sem aviso, e os limiares abaixo foram medidos com este.
MODELO_PADRAO = "jev-1.13.0"


class JevIndisponivel(QwenIndisponivel):
    """A API do Jev não respondeu, recusou a chave ou fugiu do contrato.

    Herda de ``QwenIndisponivel`` de propósito: a tool trata "o classificador
    falhou" num único ``except``, e o nome antigo ficou porque todos os chamadores
    já o capturam. Semanticamente é "classificador indisponível".
    """


@dataclass(frozen=True)
class ParametrosJev:
    """O que a tool precisa para falar com o Jev. A chave vem da config, nunca de repo."""

    chave: str
    endpoint: str = ENDPOINT_PADRAO
    modelo: str = MODELO_PADRAO
    timeout_segundos: float = 30.0


# Rubric igual ao do prompt do Qwen (classificar_samd_ia.jinja2), sem citar nenhum
# produto específico: a rodada a da avaliação citava itens da própria amostra e
# foi descartada por isso.
_CONTA_COMO_IA = (
    "rede neural, deep learning, aprendizado de máquina, modelo treinado com dados, "
    "algoritmo adaptativo, visão computacional treinada, CAD baseado em aprendizado "
    "(detecção ou diagnóstico auxiliado por computador), 'algoritmo proprietário treinado', "
    "ou a sigla IA / AI usada como nome de tecnologia do produto ou do fabricante"
)
_NAO_CONTA_COMO_IA = (
    "software de cálculo determinístico, regra fixa ou limiar configurado por humano, "
    "PACS e visualização de imagem, telemetria, prontuário eletrônico, automação sem "
    "modelo treinado, CAD/CAM de desenho e fabricação (odontologia, próteses), e as letras "
    "'AI' ou 'IA' quando são código de modelo, número de versão ou parte de uma palavra mais longa"
)

PERGUNTAS: dict[str, Any] = {
    "usa_ia": {
        "type": "noul",
        "instructions": (
            "Lendo apenas `nome_tecnico`, `nome_comercial` e `fabricante`, o texto declara "
            "ou sugere que este dispositivo médico usa inteligência artificial ou "
            "aprendizado de máquina no seu funcionamento?"
        ),
        "criteria": {
            "true": "O texto contém ou sugere: " + _CONTA_COMO_IA + ".",
            "false": (
                "O texto descreve produto físico, reagente, equipamento convencional ou "
                + _NAO_CONTA_COMO_IA
                + "; ou o texto não permite afirmar nada sobre IA."
            ),
        },
    },
    "texto_descreve_funcionamento": {
        "type": "noul",
        "instructions": (
            "Os campos `nome_tecnico`, `nome_comercial` e `fabricante` descrevem COMO o "
            "produto funciona, além de dizer o que ele é?"
        ),
        "criteria": {
            "true": "Há descrição de mecanismo, método ou tecnologia de funcionamento.",
            "false": "Só há tipo de produto, nome comercial e fabricante.",
        },
    },
}

_RE_DESCRICAO = re.compile(
    r"Nome técnico:\s*(?P<tecnico>.*?)\.\s*Nome comercial:\s*(?P<comercial>.*?)"
    r"(?:\.\s*Fabricante:\s*(?P<fabricante>.*?))?\.?\s*$",
    re.DOTALL,
)


def montar_state(nome: str, descricao: str) -> dict[str, str]:
    """Separa a descrição da Anvisa nos campos nomeados que o Jev lê melhor.

    A descrição da Anvisa segue sempre "Nome técnico: X. Nome comercial: Y.
    Fabricante: Z". Quando o padrão não casa, o texto inteiro vai como nome
    comercial, para não perder informação.
    """
    achado = _RE_DESCRICAO.match(descricao or "")
    if achado is None:
        return {"nome_tecnico": "", "nome_comercial": (descricao or nome).strip(), "fabricante": ""}
    return {
        "nome_tecnico": (achado.group("tecnico") or "").strip(),
        "nome_comercial": (achado.group("comercial") or nome).strip(),
        "fabricante": (achado.group("fabricante") or "").strip(),
    }


def resultado_de_probabilidade(
    p_usa_ia: float, p_descreve: float, modelo: str
) -> ClassificationResult:
    """Converte a probabilidade do Jev no contrato do Qwen.

    ``confianca`` é |2p - 1|, como a documentação do Jev recomenda para noul: 0,5
    vira 0 (não sabe) e 0,99 vira 0,98. É o inverso do mapa usado na avaliação
    para ler o Qwen, então os dois caches ficam comparáveis. Efeito prático: um
    "sim" com p entre 0,5 e 0,75 sai com confiança abaixo de 0,5. A tool devolve
    o item mesmo assim (o filtro de ``apenas_com_ia`` só descarta "não"), e quem
    lê deve tratar essa confiança baixa como dúvida, não como veredito.

    ``termos_citados`` fica vazio porque o Jev não cita trechos; a checagem de
    evidência devolve ``confere=True`` para lista vazia e isso é gravado, para o
    veredito não voltar ao classificador toda noite como "sem evidência".
    """
    usa_ia = p_usa_ia >= 0.5
    descreve = "descreve" if p_descreve >= 0.5 else "não descreve"
    return ClassificationResult(
        usa_ia=usa_ia,
        confianca=round(abs(2 * p_usa_ia - 1), 4),
        justificativa=(
            f"Jev: probabilidade de IA {p_usa_ia:.2f}; o texto {descreve} o funcionamento. "
            "Sem trechos citados: o modelo devolve probabilidade, não justificativa."
        ),
        termos_citados=[],
        modelo=modelo,
    )


class JevClient:
    """Cliente mínimo do endpoint systemone. Usar como ``async with``."""

    def __init__(
        self,
        parametros: ParametrosJev,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._parametros = parametros
        self._client = client
        self._client_proprio = client is None

    async def __aenter__(self) -> JevClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._parametros.timeout_segundos)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None and self._client_proprio:
            await self._client.aclose()
            self._client = None

    @property
    def modelo(self) -> str:
        return self._parametros.modelo

    def _cabecalhos(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._parametros.chave}"}

    async def esta_vivo(self) -> bool:
        """True se a API aceita a chave. GET /models não é cobrado. Nunca levanta."""
        cliente = self._exigir_client()
        url = self._parametros.endpoint.rsplit("/", 1)[0] + "/models"
        try:
            resposta = await cliente.get(url, headers=self._cabecalhos(), timeout=10.0)
            return resposta.status_code == 200
        except httpx.HTTPError as erro:
            logger.warning("Jev não respondeu em %s: %s", url, erro)
            return False

    async def classificar(self, *, nome: str, descricao: str) -> ClassificationResult:
        """Uma chamada, duas perguntas. Levanta ``JevIndisponivel`` em qualquer falha."""
        cliente = self._exigir_client()
        corpo = {
            "state": montar_state(nome, descricao),
            "model": self._parametros.modelo,
            "questions": PERGUNTAS,
        }
        try:
            resposta = await cliente.post(
                self._parametros.endpoint, json=corpo, headers=self._cabecalhos()
            )
            resposta.raise_for_status()
            dados = resposta.json()
            modelo = str(dados.get("model") or self._parametros.modelo)
            return resultado_de_probabilidade(
                float(dados["answers"]["usa_ia"]["noul"]),
                float(dados["answers"]["texto_descreve_funcionamento"]["noul"]),
                modelo,
            )
        except httpx.HTTPStatusError as erro:
            raise JevIndisponivel(
                f"Jev respondeu HTTP {erro.response.status_code} em {self._parametros.endpoint}"
            ) from erro
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as erro:
            raise JevIndisponivel(f"falha ao chamar o Jev: {erro}") from erro

    def _exigir_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("JevClient precisa ser usado como 'async with'")
        return self._client


class ClassificadorComFallback:
    """Jev primeiro; se ele falhar numa chamada, o Qwen local responde, se estiver vivo.

    O resultado carrega ``modelo`` com quem respondeu de fato, e é isso que a tool
    grava no cache. Assim dá para auditar depois quais vereditos vieram de cada um.
    """

    # Falhas seguidas do Jev que desligam a nuvem pelo resto da rodada. Sem isto, uma
    # queda no meio do sync noturno custaria um timeout de 30 s por registro, e a
    # rodada de até 5000 registros levaria mais de um dia para cair no Qwen.
    FALHAS_PARA_DESLIGAR = 3

    def __init__(self, jev: JevClient, qwen: QwenClient | None) -> None:
        self._jev = jev
        self._qwen = qwen
        self._jev_vivo = False
        self._qwen_vivo = False
        self._falhas_seguidas = 0

    async def esta_vivo(self) -> bool:
        """Vivo se qualquer um dos dois responde. Memoriza o estado dos dois."""
        self._jev_vivo = await self._jev.esta_vivo()
        self._qwen_vivo = self._qwen is not None and await self._qwen.esta_vivo()
        if not self._jev_vivo and self._qwen_vivo:
            logger.warning("Jev fora do ar na checagem inicial; esta rodada usa só o Qwen local")
        return self._jev_vivo or self._qwen_vivo

    async def classificar(
        self, *, nome: str, descricao: str, prompt_qwen: str
    ) -> ClassificationResult:
        if self._jev_vivo:
            try:
                resultado = await self._jev.classificar(nome=nome, descricao=descricao)
                self._falhas_seguidas = 0
                return resultado
            except JevIndisponivel as erro:
                if self._qwen is None or not self._qwen_vivo:
                    raise
                self._falhas_seguidas += 1
                if self._falhas_seguidas >= self.FALHAS_PARA_DESLIGAR:
                    self._jev_vivo = False
                    logger.warning(
                        "Jev falhou %d vezes seguidas (%s); desligado pelo resto da rodada, "
                        "o Qwen local responde o restante",
                        self._falhas_seguidas,
                        erro,
                    )
                else:
                    logger.warning("Jev falhou (%s); usando o Qwen local neste registro", erro)
        elif self._qwen is None or not self._qwen_vivo:
            raise JevIndisponivel("Jev fora do ar e sem Qwen local para responder")
        texto = f"Nome do produto: {nome}\n\nDocumentação:\n{descricao}".strip()
        resultado = await self._qwen.classify(texto, prompt_qwen)
        return resultado.model_copy(update={"modelo": self._qwen._modelo_para_cache()})
