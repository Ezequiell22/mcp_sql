# Instalação e configuração

## Python local

Requer Python 3.12 ou superior em macOS ou Linux:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/atlas-sql-mcp
```

SQL Server requer também `requirements-sqlserver.lock` e Microsoft ODBC Driver 18.
A [imagem Docker](docker.md) já inclui ambos os drivers. O servidor não carrega
arquivos `.env` automaticamente e não precisa de configuração de LLM.

## Connection strings

O agente fornece `connectionString` a `atlas_sql_connect`; `schemas` pode limitar
a descoberta a nomes exatos. Não é necessário arquivo de conexões.

| Banco | Formatos aceitos |
| --- | --- |
| PostgreSQL | URI `postgresql://…` ou pares libpq `host=… port=… dbname=… user=… password=…` |
| SQL Server | URI `sqlserver://…` ou `mssql://…`; pares ODBC `Server=…;Database=…;UID=…;PWD=…` |

Substitua as reticências pelos dados reais. URIs exigem percent-encoding de
caracteres especiais nas credenciais. No libpq, use a sintaxe de aspas do driver;
no ODBC, envolva valores com ponto e vírgula em chaves e escape `}` como `}}`.
SQL Server aceita aliases `Data Source`, `Initial Catalog`, `User ID` e `Password`;
chaves duplicadas são rejeitadas. A porta padrão é 1433. Instâncias nomeadas
exigem host/porta explícitos. DSNs, autenticação integrada/Entra, opções arbitrárias
e caminhos locais de credenciais/certificados fornecidos pelo usuário não são aceitos.

Uma senha é obrigatória nas strings do usuário. Use uma conta restrita à leitura,
sem superuser/BYPASSRLS no PostgreSQL ou privilégios administrativos no SQL Server.
O servidor descobre apenas tabelas/colunas acessíveis e revalida permissões na
finalização. Novos objetos autorizados exigem reconexão.

PostgreSQL remoto exige `sslmode=verify-full`; apenas endereços literais de
loopback e `localhost` permitem o fluxo local sem TLS. SQL Server sempre exige
criptografia e certificado verificado; `TrustServerCertificate=yes` é rejeitado.
A confiança na CA é configurada pelo administrador, não pela string do usuário.

Credenciais expiram após uma hora ou reinício, com limpeza periódica de 30 segundos.
Há até dez sessões por identidade e cem por processo. Elas não são devolvidas
nos resultados nem usadas como contexto de geração. O histórico do cliente está
fora desse controle.

## Opções e ambiente

| Opção | Padrão | Uso |
| --- | --- | --- |
| `--transport` | `stdio` | `streamable-http` para serviço compartilhado |
| `--host` | `127.0.0.1` | Endereço HTTP; no contêiner, use `0.0.0.0` |
| `--port` | `8091` | Porta HTTP |
| `--connections` | Ausente | Arquivo privado opcional de conexões e grants |

| Variável | Uso |
| --- | --- |
| `ATLAS_SQL_TIMEZONE` | Fuso IANA; padrão `UTC`; não reescreve SQL |
| `ATLAS_SQL_PUBLIC_URL` | Obrigatória em HTTP: URL pública HTTPS terminada em `/mcp` |
| `ATLAS_SQL_CONNECTION_HOSTS` | Endpoints HTTP permitidos, separados por vírgula, no formato exato `host:port`; vazio desabilita conexões por string |
| `PGSSLROOTCERT` | CA confiável do PostgreSQL disponível no servidor |
| `SSL_CERT_FILE` | Bundle PEM confiável para o driver SQL Server na imagem Linux |
| `PGPASSFILE` | Passfile privado para conexões PostgreSQL previamente configuradas |

## Serviço HTTP

```sh
.venv/bin/atlas-sql-mcp --transport streamable-http --host 127.0.0.1 --port 8091
```

Defina a URL pública e a lista de endpoints antes de iniciar. O ingresso externo
deve terminar HTTPS, preservar o Host público, limitar acesso e aplicar timeouts.
Origins de navegador são rejeitadas. Não há autenticação, OAuth ou validação de
tokens no MCP: todos os clientes usam `shared#anonymous` e compartilham conexões,
contextos e limites. Não há isolamento entre usuários HTTP. Para clientes ou
equipes independentes, use [implantações separadas](docker.md), com projetos,
portas e regras de ingresso específicos para cada grupo.

## Conexões administradas opcionais

Use `--connections /caminho/privado/connections.json` para conexões permanentes
por configuração. O JSON contém `connections`, uma lista de objetos com `id`,
`dialect` (`postgresql` ou `sqlserver`), `host`, `port`, `dbname`, `user`,
`passwordEnv`, `sslmode` e `grants`.

`grants` associa a identidade interna a um mapa de tabelas qualificadas por schema
e listas exatas de colunas; curingas não são aceitos. A identidade local é
`local#` seguida do usuário do sistema; na imagem é `local#atlas`; em HTTP,
`shared#anonymous`. Grants são intersectados com os privilégios reais do banco.

`passwordEnv` contém o nome de uma variável externa, nunca a senha. PostgreSQL
aceita nome vazio para lookup libpq/passfile; SQL Server exige variável e
`sslmode: "verify-full"`. O arquivo é relido a cada acesso ao catálogo, inclusive
na finalização. Mantenha-o fora do repositório ou em `connections.local.json`,
ignorado pelo Git. Não é necessário instalar objetos no banco de destino.
