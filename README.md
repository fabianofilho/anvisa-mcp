# anvisa-mcp

Servidor MCP que expõe consulta regulatória da Anvisa a um LLM: status de registro de
medicamentos e detecção de SaMD (Software as a Medical Device) recém-registrados que
usam IA. Fala com o Qwen local servido pelo ODS na mesma máquina.

## Estado: fase 1 (esqueleto validado)

As duas tools respondem, e a conexão com o LLM local foi validada. **As tools ainda
devolvem dados de exemplo** enquanto a base local não for sincronizada — toda resposta
carrega `fonte: "mock"` e um aviso explícito. Nada de mock é apresentado como registro
real.

O que falta para a fase 2: confirmar as URLs dos datasets abertos da Anvisa
(`anvisa-cli fontes` mostra o que está pendente). Nenhuma URL foi inventada.

## Rodando

```bash
uv sync                      # instala tudo num venv do projeto
cp .env.example .env
uv run anvisa-cli llm        # confirma que o LLM local responde
uv run anvisa-cli samd       # testa a tool de SaMD fora do MCP
uv run anvisa-cli medicamento dipirona
uv run anvisa-mcp            # sobe o servidor MCP no stdio
```

`make` cobre os mesmos caminhos (`make test`, `make lint`, `make typecheck`).

## LLM local

Os defaults do `.env.example` apontam para o llama.cpp servido pelo ODS nesta máquina:

| Variável | Valor | Observação |
| --- | --- | --- |
| `QWEN_ENDPOINT` | `http://127.0.0.1:11434/v1` | É a API. A porta 3000 é o Open WebUI, não serve aqui |
| `QWEN_MODEL` | `local-model` | Nome exato do modelo carregado |

Se o ODS estiver fora do ar, a tool de SaMD devolve cada item com
`classificacao.origem = "indisponivel"` e segue respondendo: o servidor MCP não trava.

## Arquitetura

Quatro camadas isoladas:

- `data/` — baixa e normaliza os datasets públicos (APScheduler agenda o sync).
- `store/` — DuckDB local, uma tabela por dataset + cache das classificações.
- `llm/` — cliente do endpoint OpenAI-compatible e a classificação de SaMD.
- `mcp_server/` — as duas tools expostas ao cliente MCP.

`config.py` (fora do desenho original) centraliza a leitura do `.env` via
pydantic-settings, para nenhuma camada ler ambiente por conta própria.

## A classificação de IA é heurística

A Anvisa não publica campo estruturado de uso de IA. A camada `llm/` preenche a lacuna
lendo o texto livre do registro, então cada item traz `confianca`, `justificativa` e
`heuristica: true`. Use como triagem, nunca como fato regulatório.

## Tools

### `consultar_status_medicamento(nome_ou_principio_ativo)`
Busca por nome comercial ou princípio ativo, sem acento e sem caixa. Devolve todos os
registros que casam — grafias variam e a ambiguidade é de quem pergunta.

### `buscar_samd_recentes(dias=90, apenas_com_ia=True)`
Dispositivos Classe III/IV registrados na janela, cada um classificado quanto a uso de
IA. A classificação fica cacheada no DuckDB: o mesmo registro não é reclassificado a
cada chamada.

As duas são independentes: nenhuma depende da outra ter rodado antes.
