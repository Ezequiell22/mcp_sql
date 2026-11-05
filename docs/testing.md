# Verificação

## Rotina local

Na raiz do repositório:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python scripts/verify.py
git diff --check
```

O verificador confere sintaxe Python, lint, formatação, links Markdown,
JSON/TOML, schemas exportados e regressões. `--checks-only` omite os testes.
`requirements.lock` contém apenas dependências de execução.
Para executar somente os testes, use `.venv/bin/pytest`.

## Integração com bancos descartáveis

Requer Docker. Os testes criam e removem seus próprios contêineres; nunca use
DSN de cliente ou contêiner existente. Dados sintéticos e payloads fictícios
ficam exclusivamente em `tests/`, fora do produto e da imagem de execução.
Removê-los eliminaria verificações de permissões, tipos e resultados esperados.

PostgreSQL:

```sh
ALLOW_SYNTHETIC_DB_WRITES=1 .venv/bin/pytest
```

Para a verificação completa, aceite previamente as licenças Microsoft e execute:

```sh
docker build --build-arg ACCEPT_EULA=Y -t atlas-sql-mcp:local .
ALLOW_SYNTHETIC_DB_WRITES=1 ACCEPT_SQLSERVER_EULA=Y \
  ATLAS_SQL_DOCKER_IMAGE=atlas-sql-mcp:local \
  .venv/bin/python scripts/verify.py --integration
```

`--integration` exige os três controles e falha se houver testes ignorados.
Sem eles, a rotina local ignora os cenários dependentes dos bancos.
`ATLAS_SQL_SQLSERVER_IMAGE=mcr.microsoft.com/mssql/server:2025-latest` seleciona
SQL Server 2025; o padrão dos testes é 2022.

## Cobertura e limites

As regressões cobrem análise estática, contratos legados/neutros, descoberta
paginada, revogação de privilégios, parâmetros, tipos, concorrência, SQL extenso,
volume de resultados e rejeição de propostas inseguras. Os fluxos com bancos
usam o protocolo MCP real e drivers; um harness independente executa consultas
sobre os dados de teste para conferir resultados. O MCP não executa essas consultas.

Os agentes desses testes são simulações determinísticas. Resultados locais não
certificam a qualidade de um LLM, qualquer ERP, carga sustentada, ingresso de
produção ou execução futura. Registre apenas verificações realmente realizadas;
contagens históricas não representam o checkout atual.

O diagnóstico `.venv/bin/atlas-sql-check --dialect postgresql /caminho/consulta.sql`
(também aceita `sqlserver`) verifica apenas parsing. Pode aceitar uma instrução
de escrita: não substitui autorização, política de leitura ou finalização MCP.
