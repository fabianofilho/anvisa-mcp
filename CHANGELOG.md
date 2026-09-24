# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/). O projeto
segue [versionamento semântico](https://semver.org/lang/pt-BR/).

## [0.1.0] - 2026-09-24

Primeira versão pública.

### O que há

- Tool `consultar_status_medicamento(termo, limite=20)`: busca registros de medicamentos
  por nome comercial, princípio ativo ou número de registro, ignorando acento e caixa.
  Devolve `total` (quantos casam na base), `retornados`, `truncado`, `coletado_em` e,
  por registro, situação (`Ativo`/`Inativo`), vencimento, detentora, categoria e
  `visto_na_ultima_coleta`.
- Tool `buscar_samd_recentes(dias=90, apenas_com_ia=True, apenas_software=True, limite=50)`:
  dispositivos Classe III/IV registrados na janela, com veredito heurístico de uso de IA
  feito por um LLM local, confiança, justificativa, checagem de evidência e origem do
  veredito (`llm`, `cache`, `nao_classificado`, `indisponivel`).
- Sync dos dois arquivos abertos da Anvisa (`anvisa-cli sync`), idempotente, com
  publicação por troca atômica (`--publicar`) e recusa de base que encolheu mais de 10%.
- Modo connector (`TRANSPORTE=streamable-http`): somente leitura, sem LLM, com limite de
  requisições por origem e global, tetos de `limite` (200) e `dias` (3650), e validação
  de `Host` para nomes públicos declarados.
- Units systemd versionadas em `deploy/` para o connector e para o sync diário.

### Mudou na preparação da versão

- Busca sem resultado numa base com dados devolve lista vazia com aviso. Antes devolvia
  os registros de exemplo (`fonte='mock'`) com o aviso falso de base não sincronizada.
  O exemplo agora só aparece com a base ausente, vazia ou travada.
- `consultar_status_medicamento` passou a receber `termo` (antes
  `nome_ou_principio_ativo`) e aceita número de registro, com ou sem pontos.
- `%` e `_` no termo são tratados como texto, não como curinga do `LIKE`.
- As duas respostas trazem `coletado_em`, com aviso quando a última coleta tem mais de
  48 horas.
- O servidor MCP nunca abre a base para escrita, nem em stdio. Em stdio ele ainda
  classifica sob demanda, mas não grava; quem grava é a CLI.
- `anvisa-cli classificar` ganhou `--publicar` e `--forcar`, e segue o mesmo caminho de
  clone e troca atômica do sync.
- `sync` e `classificar` ganharam `--reclassificar`, que refaz uma vez os vereditos
  gravados antes da checagem de evidência, inclusive os de fora da janela ou do filtro
  de software.
- `classificar` e `sync --classificar` saem com código 1 quando os vereditos não podem ser
  gravados (base travada por outro processo), em vez de relatar sucesso.
- O prompt de classificação foi para dentro do pacote (`anvisa_mcp/prompts`) e entra no
  wheel.
- Removido o agendador interno (`agendar_syncs`, `SYNC_HORA_LOCAL`, dependência
  `apscheduler`), que nunca era chamado. O timer systemd é o caminho oficial.
- Dependência do SDK corrigida para `mcp>=2.2,<3`: o código usa `mcp.server.mcpserver`.
- `pyproject.toml` declara a licença (`Apache-2.0`).
- Tetos de `limite` e `dias` nas tools e `X-Forwarded-For` só aceito de proxy confiável.

[0.1.0]: https://github.com/fabianofilho/anvisa-mcp/commits/main
