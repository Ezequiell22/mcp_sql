# Conectar clientes

Substitua `https://sql-mcp.example.com/mcp` nos modelos pela URL HTTPS real do
servidor. O endereço é um placeholder, não um serviço publicado.

| Cliente | Modelo | Destino |
| --- | --- | --- |
| Codex | [codex.toml](codex.toml) | `~/.codex/config.toml` |
| Cursor | [cursor.json](cursor.json) | `.cursor/mcp.json` ou `~/.cursor/mcp.json` |
| Claude Code | [claude-code.json](claude-code.json) | `.mcp.json` no projeto |

Adicione somente a entrada `atlas_sql`, preservando os outros servidores e
ajustes. Os clientes não carregam automaticamente os arquivos desta pasta.
Recarregue o cliente e confirme a presença de `atlas_sql_connect`,
`atlas_sql_context_get` e `atlas_sql_finalize`.

O cliente usa seu próprio LLM; não precisa instalar drivers de banco ou configurar
provedor de IA no MCP. Informe uma connection string com usuário restrito à
leitura e descreva a consulta desejada. Não armazene senhas nestes modelos.

O servidor não possui autenticação própria; o acesso é controlado externamente.
Todos os clientes HTTP compartilham a identidade `shared#anonymous`, conexões e
contextos. A configuração do cliente não cria isolamento por usuário.

Para falhas de conexão, confira URL `/mcp`, DNS, rede e confiança no certificado
HTTPS. A lista de bancos permitidos e suas CAs são configuração do servidor.
O registro do cliente não comprova conectividade com seu ambiente.

[compose.http.yaml](compose.http.yaml) é um override de implantação do servidor;
veja [Docker](../docs/docker.md) e [configuração](../docs/configuration.md).
