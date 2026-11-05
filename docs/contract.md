# Contrato MCP

Os [schemas JSON](../schemas/) definem os campos aceitos e retornados. Prefira
`contractVersion: "1.2"` para os dois bancos, com `sqlType`,
`databaseMajorVersion` e `databaseMajorVersions`. As versões legadas 1.0
(PostgreSQL, campos `postgresType`/`postgresMajorVersion`) e 1.1 (SQL Server,
campos neutros) permanecem compatíveis.

## Ferramentas

| Ferramenta | Entrada e resultado |
| --- | --- |
| `atlas_sql_connect` | Recebe `connectionString` e, opcionalmente, `schemas`. Retorna `connectionId`, dialeto, versão legada e expiração. |
| `atlas_sql_context_get` | Recebe versão, conexão e `request`; aceita foco e paginação. Retorna contexto, metadados autorizados, capacidades e cursor. |
| `atlas_sql_finalize` | Recebe exatamente `contractVersion`, `contextId`, `sql`, `parameters`, `filters`, `outputs` e `semantics`. Retorna artefato ou erro estruturado. |

Conexões previamente configuradas também podem ser usadas. Não envie
`connectionId` ou `explanation` à finalização. Declarações de saída aceitam
`key`, `label` e, opcionalmente, tipo SQL e `unit`; `lineage`, `logicalType` e
`nullable` são derivados pelo servidor.

## Descoberta e contexto

Catálogos grandes retornam primeiro um índice de nomes. `coverage: "partial"`
não significa catálogo completo. Inspecione `focus.relations` com os detalhes
tipados antes de usar suas tabelas; `focus.include: []` retorna apenas nomes.

Ao seguir `nextCursor`, preserve contexto, solicitação, conexão, foco e tamanho
de página. Para mudar de grupo, mantenha o contexto e omita o cursor anterior.
`CONTEXT_INCOMPLETE` exige inspeção; `CONTEXT_STALE` ou expiração exigem contexto
novo. O fingerprint do índice e o das dependências tipadas têm escopos distintos
e não precisam ser iguais.

## Parâmetros e artefato

PostgreSQL exige casts explícitos como `$1::date`; SQL Server usa
`CAST(@p1 AS date)`. Posições são contíguas e cada ocorrência deve participar de
predicado WHERE/HAVING/JOIN. Vincule valores por parâmetros, nunca interpolação.
O consumidor ODBC precisa mapear nomes para posições `?` com um parser adequado;
o MCP não reescreve nem executa a consulta.

Filtros declaram tipo, posição, campo e validação. Intervalos temporais usam
`from` inclusivo e `until` exclusivo, coerentes com os operadores SQL.
Inteiros e decimais em limites/defaults usam strings para preservar precisão.
Parâmetros anuláveis não são suportados; arrays exigem tipo exato e não aceitam
elementos nulos. Labels e premissas são declarações do agente.

O artefato preserva o SQL e inclui dependências, tipos, linhagem, hash e evidências.
`nullable: null` significa desconhecido. SQL Server informa
`syntax: "passed_static_subset"`; não há validação pelo compilador nativo.
O hash depende da representação contratual: não compare hashes entre versões
como se fossem equivalentes.

Antes de executar, um consumidor deve reautorizar dependências, conferir sua
atualidade e aplicar limites próprios. PostgreSQL exige `search_path=pg_catalog`;
SQL Server exige `QUOTED_IDENTIFIER`, `ANSI_NULLS`, `ANSI_WARNINGS` e `ARITHABORT`
em `ON`. O artefato não concede permissão de execução.

Após alterar o contrato, regenere os schemas com
`.venv/bin/python -m atlas_sql_mcp.contracts schemas` e execute a [verificação](testing.md).
