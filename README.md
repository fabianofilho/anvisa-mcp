# anvisa-mcp

Servidor MCP que expõe consulta regulatória da Anvisa a um LLM: status de registro de
medicamentos e detecção de SaMD (Software as a Medical Device) recém-registrados que
usam IA. Fala com o Qwen local servido pelo ODS na mesma máquina.

## Estado

As duas tools respondem, a conexão com o LLM local foi validada e as fontes de dados da
Anvisa estão confirmadas. Rode `anvisa-cli sync` para popular a base local; enquanto ela
estiver vazia, as tools devolvem dados de exemplo com `fonte: "mock"` e aviso explícito.
Nada de mock é apresentado como registro real.

## Fontes de dados

Confirmadas em 2026-09-20 baixando os arquivos e lendo o cabeçalho real — não deduzidas
de documentação. O índice publicado fica em <https://dados.anvisa.gov.br/dados/>.

| Dataset | Arquivo | Tamanho |
| --- | --- | --- |
| Medicamentos registrados | `DADOS_ABERTOS_MEDICAMENTOS.csv` | ~8 MB |
| Produtos para saúde (dispositivos médicos) | `TA_PRODUTO_SAUDE_SITE.csv` | ~28 MB |

Ambos em ISO-8859-1, separador `;`, atualizados diariamente (D-1).

Duas escolhas que valem explicação:

- `TA_PRODUTO_SAUDE_MODELO.csv` traz descrição por modelo, que seria um texto melhor para
  classificar, mas tem **1 GB** e não é atualizado desde dezembro de 2025. Ficou de fora.
- `TA_CONSULTA_PRODUTOS_SAUDE.CSV` (53 MB) é um dump de ETL com 24 colunas de processo;
  `TA_PRODUTO_SAUDE_SITE.csv` é o mesmo registro em forma limpa. Ficou o segundo.

**Limite conhecido:** o arquivo de produtos para saúde não tem campo de descrição livre.
O texto que alimenta a classificação de IA é montado de nome técnico, nome comercial e
fabricante — é pouco, e a confiança devolvida reflete isso. Um produto cujo nome não diga
"software" ou "sistema" dificilmente será classificado com confiança alta.

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

## Base local e concorrência

O DuckDB é um arquivo com **um escritor por vez**, e enquanto o sync escreve ele bloqueia
até leitores. Por isso:

- As tools abrem a base em **modo leitura**; o sync é o único escritor.
- Durante um sync, as tools caem para os dados de exemplo — mas o aviso diz que a base
  está ocupada, não que ela está vazia. As duas situações são diferentes e a resposta
  precisa distinguir.
- O cache de classificação é gravado **depois** de fechar a conexão de leitura: o DuckDB
  recusa abrir escrita e leitura no mesmo arquivo dentro do mesmo processo.

O sync carrega os registros numa tabela temporária e faz um único upsert em massa. Com
upsert linha a linha, os ~133 mil registros levavam 4min27; em lote, 52s.

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

### `buscar_samd_recentes(dias=90, apenas_com_ia=True, apenas_software=True)`
Dispositivos Classe III/IV registrados na janela, cada um classificado quanto a uso de
IA. A classificação fica cacheada no DuckDB: o mesmo registro não é reclassificado a
cada chamada.

**Por que existe o `apenas_software`:** no último ano há 1.832 registros Classe III/IV, e
só 13 mencionam software. Ordenando por data, os primeiros são cânulas, parafusos e
testes rápidos — numa varredura de 60 registros recentes, zero eram software. O filtro
por palavra-chave manda as chamadas de LLM para candidatos plausíveis. É recall trocado
por custo: um produto que use IA sem dizer "software", "algoritmo", "CAD" e afins fica de
fora, e o aviso da resposta diz isso. Use `apenas_software=False` para varrer tudo.

As duas são independentes: nenhuma depende da outra ter rodado antes.
