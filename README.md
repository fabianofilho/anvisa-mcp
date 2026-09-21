# anvisa-mcp

Servidor MCP que expõe duas consultas à base de dados abertos da Anvisa a um LLM: status
de registro de medicamentos e busca de dispositivos médicos recentes, com uma tentativa de
classificar quais deles usam IA. Roda local, contra um LLM que você mesmo hospeda.

> ### ⚠️ Não é fonte oficial
>
> - **Não substitui a consulta ao portal da Anvisa.** Os dados vêm dos arquivos abertos
>   publicados pela agência e podem estar defasados em relação ao sistema oficial.
> - **A classificação de uso de IA é heurística**, feita por um LLM lendo texto livre. A
>   Anvisa não publica campo estruturado sobre isso. Cada resposta traz confiança e
>   justificativa justamente porque o veredito pode estar errado.
> - **Não houve validação clínica ou regulatória** deste software. Ele não foi avaliado
>   por nenhum órgão e não é um dispositivo médico.
> - Para qualquer decisão regulatória, use o registro oficial no portal da Anvisa.

## Requisitos

| O quê | Versão | Para quê |
| --- | --- | --- |
| Python | 3.12+ | runtime |
| [uv](https://docs.astral.sh/uv/) | recente | dependências e venv |
| Um LLM local com API OpenAI-compatible | — | classificar dispositivos (llama.cpp, Ollama, LM Studio) |
| Espaço em disco | ~500 MB | base DuckDB (~42 MB) + dependências |

Não é preciso servidor de banco: o DuckDB é um arquivo.

## Instalação

```bash
git clone https://github.com/fabianofilho/anvisa-mcp.git
cd anvisa-mcp
uv sync
cp .env.example .env
```

## Configuração

Edite o `.env` com o endereço do seu LLM:

| Variável | Padrão | Observação |
| --- | --- | --- |
| `QWEN_ENDPOINT` | `http://127.0.0.1:8080/v1` | llama.cpp. Ollama: `:11434/v1`. LM Studio: `:1234/v1` |
| `QWEN_MODEL` | `local-model` | llama.cpp e LM Studio aceitam qualquer nome; o Ollama exige o exato |
| `DUCKDB_PATH` | `./data/anvisa.duckdb` | onde a base local fica |
| `SYNC_HORA_LOCAL` | `03:20` | horário fixo do sync agendado |
| `QWEN_TIMEOUT_SEGUNDOS` | `120` | timeout por chamada ao LLM |

Confirme que o LLM responde e baixe os dados:

```bash
uv run anvisa-cli llm     # deve imprimir a resposta do modelo
uv run anvisa-cli sync    # ~1 min: baixa os dois CSVs da Anvisa
uv run anvisa-cli schema  # contagens da base
```

O `sync` é idempotente: rode de novo quando quiser atualizar. A Anvisa republica os
arquivos diariamente (D-1).

### Ligando ao Claude Code

Troque `/caminho/para/anvisa-mcp` pelo caminho real do clone. Os caminhos precisam ser
**absolutos**: o servidor é lançado de qualquer diretório, então `./data/anvisa.duckdb`
relativo não resolveria.

```bash
claude mcp add anvisa --scope user \
  -e DUCKDB_PATH=/caminho/para/anvisa-mcp/data/anvisa.duckdb \
  -e QWEN_ENDPOINT=http://127.0.0.1:8080/v1 \
  -e QWEN_MODEL=local-model \
  -- uv --directory /caminho/para/anvisa-mcp run anvisa-mcp
```

## Uso

### `consultar_status_medicamento(nome_ou_principio_ativo: str)`

Busca por nome comercial ou princípio ativo, ignorando acento e caixa. Devolve todos os
registros que casam — grafias variam e a ambiguidade é de quem pergunta. Registros ativos
vêm primeiro: dois terços da base são inativos.

```json
{
  "termo_consultado": "dipirona",
  "fonte": "duckdb",
  "total": 20,
  "resultados": [
    {
      "numero_registro": "100430233",
      "nome_produto": "DIPBE",
      "principio_ativo": "dipirona",
      "empresa_detentora": "...",
      "situacao": "Ativo",
      "data_situacao": "2031-07-01",
      "categoria": "Similar"
    }
  ]
}
```

### `buscar_samd_recentes(dias=90, apenas_com_ia=True, apenas_software=True)`

Dispositivos Classe III/IV registrados na janela, classificados quanto a uso de IA.

```json
{
  "dias": 1825,
  "fonte": "duckdb",
  "total": 1,
  "indeterminados": 12,
  "resultados": [
    {
      "nome_produto": "CAD4TB",
      "classe_risco": "III",
      "classificacao": {
        "usa_ia": true,
        "confianca": 0.6,
        "justificativa": "O nome sugere CAD (Computer-Aided Diagnosis) e o fabricante é Delft AI.",
        "origem": "llm",
        "heuristica": true
      }
    }
  ]
}
```

A classificação fica cacheada no DuckDB: o mesmo registro não é reclassificado a cada
chamada (13,4s na primeira vez, instantâneo depois).

## Limitações conhecidas

**O registro de dispositivos não tem campo de descrição.** O texto que alimenta a
classificação é nome técnico + nome comercial + fabricante. É pouco. Um produto cujo nome
não diga o que ele faz será classificado com confiança baixa — e confiança baixa significa
*"não dá para saber"*, não *"não usa IA"*. Por isso a resposta traz `indeterminados`.

**`apenas_software=True` troca recall por custo.** No último ano há 1.832 registros Classe
III/IV e só 13 mencionam software. Sem esse pré-filtro por palavra-chave, uma varredura
dos 60 registros mais recentes encontra zero software — são cânulas, parafusos e testes
rápidos. Com ele, um produto que use IA sem dizer "software", "algoritmo" ou "CAD" fica de
fora. Use `apenas_software=False` para varrer tudo, ao custo de uma chamada de LLM por
registro.

**A calibração de confiança do LLM é parcial.** Modelos pequenos tendem a responder com
confiança alta mesmo quando o texto não sustenta. O prompt fixa faixas de confiança por
situação, o que melhorou bastante (reagentes saem com 0,9, nomes ambíguos com 0,2), mas
não resolve por completo.

**Medicamentos sem número de registro não entram.** O arquivo inclui produtos notificados
(baixo risco), que não têm registro — e a pergunta "qual o status do registro" não se
aplica a eles.

**As URLs dos datasets podem mudar.** Elas estão em `data/sources.py`, confirmadas em
20/09/2026 baixando os arquivos. Se uma sair do ar, o sync falha com mensagem explicando
como reconfirmar, em vez de baixar o arquivo errado em silêncio.

**O DuckDB aceita um escritor por vez.** Durante o `sync`, as tools não conseguem ler e
caem para dados de exemplo — mas o aviso diz que a base está *ocupada*, não vazia.

## Privacidade

- **Sai da máquina:** requisições HTTP para `dados.anvisa.gov.br`, para baixar os CSVs
  públicos. Nada mais.
- **Não sai:** os termos que você consulta. A busca roda contra a base local.
- O LLM é o seu: o texto dos registros vai para o endpoint que você configurou.
- Sem telemetria, sem analytics, sem coleta de uso.

## Contribuindo

Veja [CONTRIBUTING.md](CONTRIBUTING.md). Em resumo: rode `pytest`, `ruff` e `mypy` antes do
PR, e não rode sincronizações em loop contra os dados abertos da Anvisa.

## Licença e atribuição

[Apache License 2.0](LICENSE) — escolhida por tocar em regulação de dispositivo médico,
onde a cláusula explícita de patente é mais protetiva.

Construído no contexto do [IA.med](https://iamed.cc).
