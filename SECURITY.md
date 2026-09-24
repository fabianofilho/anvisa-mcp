# Segurança

## Como reportar

Não abra issue pública para vulnerabilidade. Use o
[aviso privado de segurança do GitHub](https://github.com/fabianofilho/anvisa-mcp/security/advisories/new)
deste repositório, com os passos para reproduzir e o impacto que você observou.

É um projeto mantido por uma pessoa: a resposta vem assim que possível, sem prazo
garantido.

## Escopo

Dentro do escopo:

- o código deste repositório (servidor MCP, CLI, sync, camada de armazenamento);
- as units de `deploy/`;
- o connector público em `https://mcp.tailf42a96.ts.net/anvisa/mcp`, que roda este código.

Fora do escopo:

- os dados em si: vêm dos arquivos abertos da Anvisa e erros neles devem ser reportados
  à agência;
- o veredito de uso de IA, que é heurístico por construção (ver README);
- negação de serviço por volume contra o connector público, que já tem limite de taxa e
  não tem garantia de disponibilidade;
- o LLM local de quem roda o projeto e a infraestrutura de terceiros (Tailscale, portal
  da Anvisa).
