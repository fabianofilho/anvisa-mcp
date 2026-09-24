# anvisa-mcp

Servidor MCP que expõe duas consultas à base de dados abertos da Anvisa a um LLM: status
de registro de medicamentos e busca de dispositivos médicos recentes, com uma tentativa de
classificar quais deles usam IA. Roda local, contra um LLM que você mesmo hospeda, ou
pelo connector público descrito abaixo.

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

## Connector público

Há uma instância rodando este código, aberta, que dá para adicionar ao Claude como
custom connector sem instalar nada:

```
https://mcp.tailf42a96.ts.net/anvisa/mcp
```

Condições de uso:

- **Dados públicos da Anvisa, sem garantia.** É a mesma base que o sync abaixo monta a
  partir dos arquivos abertos da agência, atualizada uma vez por dia de madrugada. Cada
  resposta traz `coletado_em`, a hora da última coleta, e avisa quando ela passou de 48
  horas. Não substitui o portal da Anvisa (ver o aviso acima).
- **Sem garantia de disponibilidade.** Roda numa máquina pessoal atrás do Tailscale
  Funnel. Pode sair do ar, reiniciar ou mudar de endereço sem aviso.
- **Limites de taxa:** 600 requisições por minuto por origem e 1.200 por minuto no total.
  Acima disso a resposta é HTTP 429 por um minuto.
- **Teto dos parâmetros:** `limite` de 1 a 200 nas duas tools e `dias` de 1 a 3650.
  Valores fora da faixa são recusados.
- **Somente leitura e sem LLM na hora:** o connector não classifica sob demanda. Ele
  serve os vereditos que a coleta diária gravou, que cobrem os dispositivos dos últimos
  10 anos que passam no filtro de software. O resto vem como `nao_classificado`.
- **Os termos consultados chegam ao servidor**, diferente do uso local. O código não
  registra os termos nem mantém log de acesso (o uvicorn roda em nível `warning`); o que
  vai para o log é a origem da requisição quando ela passa do limite de taxa.

## Requisitos

| O quê | Versão | Para quê |
| --- | --- | --- |
| Python | 3.12+ | runtime |
| [uv](https://docs.astral.sh/uv/) | recente | dependências e venv |
| Um LLM local com API OpenAI-compatible | - | classificar dispositivos (llama.cpp, Ollama, LM Studio) |
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
| `QWEN_TIMEOUT_SEGUNDOS` | `120` | timeout por chamada ao LLM |

Confirme que o LLM responde e baixe os dados:

```bash
uv run anvisa-cli llm     # deve imprimir a resposta do modelo
uv run anvisa-cli sync    # ~1 min: baixa os dois CSVs da Anvisa
uv run anvisa-cli schema  # contagens da base
```

O `sync` é idempotente: rode de novo quando quiser atualizar. A Anvisa republica os
arquivos diariamente (D-1). O código não agenda nada sozinho: para rodar todo dia, use o
timer systemd de `deploy/` (ver [Rodar como serviço](#rodar-como-serviço)) ou o agendador
que preferir chamando `anvisa-cli sync`.

Para classificar os dispositivos e gravar os vereditos na base:

```bash
uv run anvisa-cli classificar --dias 365   # só o que ainda não tem veredito
```

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

### `consultar_status_medicamento(termo: str, limite: int = 20)`

Busca por trecho do nome comercial ou do princípio ativo, ignorando acento e caixa, ou
pelo número de registro completo, com ou sem pontos (`1.1819.0006` e `118190006` dão o
mesmo resultado; o número de 13 dígitos da apresentação também serve). Devolve todos os
registros que casam: grafias variam e a ambiguidade é de quem pergunta. Registros ativos
vêm primeiro, porque dois terços da base são inativos.

`limite` vai de 1 a 200. `total` é quantos casam na base inteira, e `truncado=true` diz
que há mais do que veio: "dipirona" casa com 557 registros, um por detentor e
apresentação. `situacao` vem como a Anvisa publica, `Ativo` ou `Inativo`, e
`data_situacao` é a data de vencimento do registro.

Saída real, de 24/09/2026:

```json
{
  "termo_consultado": "1.1819.0006",
  "fonte": "duckdb",
  "coletado_em": "2026-09-24T03:22:04.857697",
  "total": 1,
  "retornados": 1,
  "truncado": false,
  "resultados": [
    {
      "numero_registro": "118190006",
      "nome_produto": "DELTALAB",
      "principio_ativo": "deltametrina",
      "empresa_detentora": "92265552000905 - MULTILAB INDUSTRIA E COMERCIO DE PRODUTOS FARMACEUTICOS LTDA",
      "situacao": "Ativo",
      "data_situacao": "2029-02-01",
      "categoria": "Similar",
      "visto_na_ultima_coleta": true
    }
  ],
  "aviso": null
}
```

Termo sem correspondência devolve `resultados: []`, `total: 0` e um aviso. Dados de
exemplo, com `fonte: "mock"` e nomes terminados em "(EXEMPLO MOCK)", só aparecem quando a
base não existe, está vazia ou não pôde ser aberta.

### `buscar_samd_recentes(dias: int = 90, apenas_com_ia: bool = True, apenas_software: bool = True, limite: int = 50)`

Dispositivos Classe III/IV registrados na janela, com um veredito heurístico sobre uso de
IA. `dias` vai de 1 a 3650 e `limite` de 1 a 200. Trecho de uma saída real, de
24/09/2026, no connector (`dias=3650`, `limite=200`):

```json
{
  "dias": 3650,
  "apenas_com_ia": true,
  "fonte": "duckdb",
  "coletado_em": "2026-09-24T03:22:40.940760",
  "total": 310,
  "analisados": 200,
  "retornados": 1,
  "truncado": true,
  "indeterminados": 158,
  "resultados": [
    {
      "numero_registro": "80102513702",
      "nome_produto": "CAD4TB",
      "classe_risco": "III",
      "situacao": "29/06/2036",
      "validade_registro": "2036-06-29",
      "data_registro": "2026-06-29",
      "classificacao": {
        "usa_ia": true,
        "confianca": 0.6,
        "justificativa": "O nome 'CAD4TB' sugere uso de CAD (Computer-Aided Diagnosis) para tuberculose, e o fabricante 'DELFT AI B.V.' indica forte probabilidade de uso de IA, mas a documentação fornecida é insuficiente para confirmar o funcionamento técnico.",
        "evidencia_confere": null,
        "origem": "cache",
        "heuristica": true
      }
    }
  ]
}
```

`total` é o universo da janela depois do filtro de software, `analisados` é quantos esta
chamada olhou e `indeterminados` conta os que ficaram de fora por confiança baixa ou por
não terem veredito, que não é o mesmo que "não usa IA". `evidencia_confere` é `false`
quando o modelo citou como evidência algo que não está no registro, e `null` nos
vereditos gravados antes dessa checagem existir.

O servidor MCP **nunca grava na base**, em nenhum modo. Em stdio, com o LLM no ar, ele
classifica na hora o que não está no cache e avisa que o veredito não ficou gravado; para
gravar, rode `anvisa-cli classificar`. Assim um cliente stdio apontado para a mesma base de
um connector não disputa o lock de escrita com ele.

## Modo connector (servidor HTTP)

Por padrão o servidor fala **stdio**: o cliente sobe o processo na máquina de quem usa.
Com `TRANSPORTE=streamable-http`, ele vira um servidor alcançável pela rede, que é o que
o Claude aceita como custom connector.

```bash
TRANSPORTE=streamable-http HTTP_HOST=127.0.0.1 HTTP_PORTA=8000 uv run anvisa-mcp
```

Ele escuta no loopback. Para expor, ponha um proxy reverso com TLS ou um túnel na frente
(ver [abaixo](#atrás-de-um-proxy-ou-túnel-declare-o-nome-público)).

| Variável | Padrão | Observação |
| --- | --- | --- |
| `TRANSPORTE` | `stdio` | `streamable-http` liga o modo connector |
| `HTTP_HOST` / `HTTP_PORTA` / `HTTP_PATH` | `127.0.0.1` / `8000` / `/mcp` | |
| `HTTP_STATELESS` | `true` | cada requisição independente; escala melhor |
| `HTTP_LIMITE_GLOBAL_POR_MINUTO` | `1200` | o teto que protege a máquina |
| `HTTP_LIMITE_POR_MINUTO` | `600` | por origem, contra chamada direta |

**Por que dois limites.** Quando o Claude chama um connector remoto, as requisições chegam
dos **IPs da Anthropic**, não do usuário final. Limitar só por IP colocaria todos os
usuários no mesmo balde: ou derruba todo mundo junto, ou não protege nada. O teto global é
o que vale para esse tráfego; o por origem serve contra quem chama o servidor direto.

### No modo connector o servidor não chama o LLM

`buscar_samd_recentes` normalmente pede a um LLM local que leia o texto do registro e
julgue se o produto usa IA. Num connector isso não se sustenta: cada usuário pagaria a
espera de uma fila de GPU compartilhada, e a classificação grava no DuckDB, que recusa
abrir para escrita enquanto houver leitor.

Então, com `TRANSPORTE=streamable-http`, o servidor **serve só o que já está no cache** e
diz isso na resposta. Quem classifica é a coleta, fora do processo do servidor:

```bash
uv run anvisa-cli sync --publicar --classificar --dias-classificacao 3650
```

`--classificar` é o único momento em que o LLM entra num deploy de connector. Sem ele a
coleta atualiza os registros mas não julga os novos, e eles chegam ao servidor como
`nao_classificado`. A janela padrão de `--dias-classificacao` é 365 dias; o deploy oficial
usa 3650 para que as consultas de até 10 anos venham classificadas. Fora da janela, o
esperado é `nao_classificado`.

Cada item traz de onde veio o veredito, em `origem_classificacao`:

| Valor | Significado |
| --- | --- |
| `llm` | classificado agora (só acontece em stdio, e não fica gravado) |
| `cache` | classificado numa coleta anterior |
| `nao_classificado` | connector sem o item no cache: **indeterminado**, não é "não usa IA" |
| `indisponivel` | LLM configurado mas fora do ar |

A diferença importa. Um registro que ninguém classificou ainda não é um registro sem IA, e
a resposta nunca conta os dois juntos.

### Atrás de um proxy ou túnel, declare o nome público

O SDK do MCP valida o cabeçalho `Host` e responde **421 Invalid Host** ao que não
reconhece. É proteção contra DNS rebinding, um ataque em que um site qualquer faz o
navegador da vítima conversar com um servidor que só deveria ser local.

Quando o servidor fica atrás de um túnel, o `Host` que chega é o nome público, não
`127.0.0.1`, e toda requisição legítima leva 421. A saída certa é declarar o nome, não
desligar a checagem:

```bash
HTTP_HOSTS_PUBLICOS=mcp.exemplo.ts.net
```

Aceita vários separados por vírgula. O loopback continua valendo junto, porque é assim
que se testa o servidor de dentro da máquina.

### Rodar como serviço

`deploy/` traz as três units de usuário que rodam o connector oficial:

| Unit | O que faz |
| --- | --- |
| `anvisa-connector.service` | servidor HTTP na porta 8101, só loopback, `Restart=always` |
| `anvisa-sync.service` | `sync --publicar --classificar --dias-classificacao 3650 --reclassificar` |
| `anvisa-sync.timer` | dispara o sync todo dia às 03:20, com até 30 min de espalhamento |

Elas esperam o repositório em `~/anvisa-mcp` e o uv em `~/.local/bin`; ajuste os caminhos
se o seu layout for outro. Em `anvisa-connector.service`, troque `HTTP_HOSTS_PUBLICOS` pelo
nome público da sua instância. Para instalar:

```bash
cp deploy/anvisa-connector.service deploy/anvisa-sync.service deploy/anvisa-sync.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now anvisa-connector.service anvisa-sync.timer
```

O connector **escuta só em 127.0.0.1**. Expor para fora é uma camada à parte, um proxy
reverso com TLS ou um túnel, que aponta para essa porta. Manter assim deixa a decisão de
expor num lugar só, em vez de espalhada em variável de ambiente. Se o processo morrer, o
systemd sobe de novo em 5 segundos.

O connector só lê. Quem escreve é o sync, que roda separado e troca o arquivo por rename.
`--reclassificar` refaz uma vez os vereditos gravados antes da checagem de evidência
(`evidencia_confere` nulo), inclusive os que estão fora da janela ou do filtro de software;
depois que todos passaram por ela, a opção não custa nada.

Não há aviso externo quando o sync falha: o connector segue servindo a base anterior, e a
falha aparece em `journalctl --user -u anvisa-sync` e no `coletado_em` das respostas, que
ganham aviso quando a coleta passa de 48 horas.

**Risco conhecido: o connector roda o código da árvore de trabalho.** O `ExecStart` sobe o
que estiver em `~/anvisa-mcp` no momento de cada restart, inclusive o automático. Se essa
pasta também for onde se desenvolve, trocar de branch ali muda o que está em produção no
próximo restart. Para isolar, aponte a unit para um checkout dedicado, por exemplo um `git
worktree` fixo na `main`, mantendo `DUCKDB_PATH` na base que o sync publica.

### A base não vai junto, e o sync roda fora

O DuckDB recusa abrir para escrita enquanto houver um leitor, e no modo connector o
servidor abre a base a cada requisição. Escrever direto no arquivo servido falharia sempre
que a coleta caísse em cima de uma consulta.

Por isso o sync usa `--publicar`: constrói a base ao lado e troca por `os.replace`, que é
atômico no POSIX. Quem já abriu continua no arquivo antigo até fechar, o tempo de uma
requisição; quem abrir depois pega o novo.

**A base ao lado começa como cópia da que está sendo servida, não vazia.** Nem tudo na base
vem do dataset: o cache de classificações custou uma chamada de LLM por linha, e os
registros que sumiram do arquivo da Anvisa continuam lá, marcados como não vistos na última
coleta. Construir do zero jogaria os dois fora. Medido uma vez, sem a cópia: 109
classificações e 13 registros a menos, publicados sem um aviso. A cópia é feita pelo próprio
DuckDB (`COPY FROM DATABASE`), porque um `cp` pegaria o arquivo sem o WAL pendente.

```bash
uv run anvisa-cli sync --publicar --classificar      # coleta, julga os novos e troca no fim
uv run anvisa-cli classificar --publicar --dias 365  # só enche o cache, sem recoletar
uv run anvisa-cli sync --publicar --forcar           # aceita base menor que a servida
```

Com um connector lendo a base, use sempre `--publicar`, também no `classificar`: sem ele
a CLI tenta escrever direto no arquivo servido, o DuckDB recusa, e ela sai com código 1
dizendo que a classificação não foi gravada.

**A publicação é recusada quando a base nova encolhe mais de 10%.** Com a cópia acima, uma
coleta interrompida já não produz base pequena: ela só deixa de atualizar. A checagem fica
como rede de segurança para o que a cópia não cobre, como um clone que falhou pela metade
ou uma remoção em massa vinda da fonte. A versão trocada fica como `.anterior`, e
`store.troca.reverter()` volta atrás.

O repositório **não traz a base pronta**. Quem clona roda o próprio sync; quem hospeda um
connector serve a sua. Os dados vêm do portal de dados abertos da Anvisa, que é público,
então qualquer pessoa consegue montar a sua, mas a base construída e classificada é
trabalho de organização, não faz parte do código.

## Limitações conhecidas

**O registro de dispositivos não tem campo de descrição.** O texto que alimenta a
classificação é nome técnico + nome comercial + fabricante. É pouco. Um produto cujo nome
não diga o que ele faz será classificado com confiança baixa, e confiança baixa significa
*"não dá para saber"*, não *"não usa IA"*. Por isso a resposta traz `indeterminados`.

**`apenas_software=True` troca recall por custo.** No último ano há 1.832 registros Classe
III/IV e só 13 mencionam software. Sem esse pré-filtro por palavra-chave, uma varredura
dos 60 registros mais recentes encontra zero software, são cânulas, parafusos e testes
rápidos. Com ele, um produto que use IA sem dizer "software", "algoritmo" ou "CAD" fica de
fora. Use `apenas_software=False` para varrer tudo, ao custo de uma chamada de LLM por
registro.

**A calibração de confiança do LLM é parcial.** Modelos pequenos tendem a responder com
confiança alta mesmo quando o texto não sustenta. O prompt fixa faixas de confiança por
situação, o que melhorou bastante (reagentes saem com 0,9, nomes ambíguos com 0,2), mas
não resolve por completo.

**A base guarda o último estado visto, e não remove nada.** O sync é upsert: um registro
que a Anvisa tira da publicação continua na base com a situação da última vez que apareceu.
Registro que sai da publicação costuma ter sido cancelado, então cada resultado traz
`visto_na_ultima_coleta`, e a resposta ganha um aviso quando algum vier `false`. Aconteceu
de verdade: entre 20 e 21/09/2026, **13 dispositivos sumiram** do arquivo publicado.

`anvisa-cli schema` mostra a contagem, e o sync loga um aviso quando há registros assim.

**Medicamentos sem número de registro não entram.** O arquivo inclui produtos notificados
(baixo risco), que não têm registro, e a pergunta "qual o status do registro" não se
aplica a eles.

**As URLs dos datasets podem mudar.** Elas estão em `data/sources.py`, confirmadas em
20/09/2026 baixando os arquivos. Se uma sair do ar, o sync falha com mensagem explicando
como reconfirmar, em vez de baixar o arquivo errado em silêncio.

**O DuckDB aceita um escritor por vez.** Com `--publicar`, o sync e o `classificar`
escrevem numa cópia e as consultas seguem lendo a base servida o tempo todo. Sem
`--publicar`, enquanto a CLI escreve na base, as tools não conseguem abri-la e caem para
dados de exemplo, com um aviso que diz que a base está *ocupada*, não vazia.

**A classificação cobre uma janela.** Só os dispositivos dentro de `--dias-classificacao`
que passam no filtro de software recebem veredito na coleta. No connector oficial a janela
é de 10 anos; numa instância com o padrão, 365 dias.

**Parte dos vereditos antigos não passou pela checagem de evidência.** Eles vêm com
`evidencia_confere: null` até o sync com `--reclassificar` refazê-los. Essa passada parte
do próprio cache, sem janela nem filtro de software, então alcança também os vereditos
gravados por consultas com `apenas_software=False`. Se o LLM estiver fora nessa hora, o
veredito antigo continua servindo e volta na coleta seguinte.

## Privacidade

Rodando local:

- **Sai da máquina:** requisições HTTP para `dados.anvisa.gov.br`, para baixar os CSVs
  públicos. Nada mais.
- **Não sai:** os termos que você consulta. A busca roda contra a base local.
- O LLM é o seu: o texto dos registros vai para o endpoint que você configurou.
- Sem telemetria, sem analytics, sem coleta de uso.

Pelo connector público, os termos e parâmetros de cada consulta chegam ao servidor, como
em qualquer serviço remoto.

## Contribuindo

Veja [CONTRIBUTING.md](CONTRIBUTING.md). Em resumo: rode `pytest`, `ruff` e `mypy` antes do
PR, e não rode sincronizações em loop contra os dados abertos da Anvisa.

## Licença e atribuição

[Apache License 2.0](LICENSE): escolhida por tocar em regulação de dispositivo médico,
onde a cláusula explícita de patente é mais protetiva.

Construído no contexto do [IA.med](https://iamed.cc).
