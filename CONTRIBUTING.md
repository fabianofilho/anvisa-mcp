# Contribuindo

Obrigado pelo interesse. Este e um projeto pequeno, mantido por uma pessoa so, entao
issues e PRs objetivos sao os mais faceis de tratar.

## Rodando localmente

Requer [uv](https://docs.astral.sh/uv/) e Python 3.12+.

```bash
uv sync
cp .env.example .env     # ajuste o endpoint do seu LLM local
uv run pytest -q         # testes
uv run ruff check .      # lint
uv run ruff format .     # formatacao
uv run mypy              # tipos
```

Os testes rodam **offline**: as respostas do portal da Anvisa e do LLM sao simuladas com
`respx`, e as bases DuckDB sao criadas em diretorio temporario pelos proprios testes. Nao e
preciso rede nem LLM para testar.

## Padrao de commit

Assunto no imperativo, em uma linha curta, seguido de um corpo explicando **por que** a
mudanca e necessaria. Se a mudanca veio de um comportamento observado (um parser que
quebrou, uma API que respondeu diferente), descreva o caso concreto.

Antes de abrir o PR, rode os quatro comandos acima. O CI roda os mesmos.

## Nao rode sincronizacao em loop

Os dados abertos da Anvisa sao servicos publicos e gratuitos, mantidos com dinheiro publico e
nao dimensionados para volume automatizado.

- Nao rode o sync em loop, nem reduza o intervalo entre requisicoes para testar.
- Para desenvolver e testar, simule as respostas com `respx`, como os testes existentes
  fazem, em vez de bater no portal de verdade.
- Se precisar de uma coleta real durante o desenvolvimento, rode uma fonte so
  (`anvisa-cli sync --fonte medicamentos`) e uma vez, nao em laco.
- Um PR que aumente a frequencia de acesso as fontes precisa justificar por que.
