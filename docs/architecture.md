# Arquitetura e limites

## Fluxo

1. O agente chama `atlas_sql_connect` com a connection string do usuário.
2. `atlas_sql_context_get` retorna metadados autorizados, capacidades e contexto
   temporário. O agente percorre as páginas e inspeciona as tabelas necessárias.
3. O agente esclarece ambiguidades, escreve SQL e declara filtros e resultados.
4. `atlas_sql_finalize` verifica sintaxe, dependências, permissões, tipos,
   parâmetros e regras estruturais. Retorna um artefato ou erro explícito.

O único LLM é o agente conectado. O servidor não possui provedor de modelos,
chave de API, geração interna, API de execução ou ciclo de vida de relatórios.

## Componentes

- `domain.py`: modelos e portas independentes de MCP e drivers, com `sql_type` e `database_major` neutros.
- `application/`: conexões temporárias, contexto e finalização; `context.py` mantém handles e orçamentos, e `policy.py` concentra capacidades por dialeto.
- `metadata/`: consultas fixas aos catálogos PostgreSQL e SQL Server.
- `analysis/`: análise estática isolada, tipos, dependências e bindings.
- `transport/`: ferramentas MCP por stdio ou Streamable HTTP.
- `contracts.py` e [schemas](../schemas/): contrato executável e exportações JSON.

PostgreSQL usa pglast para sintaxe nativa e SQLGlot para resolução e linhagem;
as dependências encontradas devem concordar. SQL Server usa um analisador
estático baseado em SQLGlot, não o compilador nativo do banco.

## Segurança

O acesso ao destino se limita a consultas de catálogo autorizadas. O produto
não executa SQL gerado, `PREPARE`, `EXPLAIN` nem instala objetos auxiliares.
Solicitações, SQL e comentários do banco são dados não confiáveis; comentários
não concedem permissões. Sintaxe, tipos e dependências desconhecidos falham.

A finalização revalida privilégios e metadados. Credenciais não entram no
contexto de geração, resultados ou diagnósticos. Sessões ficam apenas na memória.
Em stdio, a identidade vem do usuário do sistema operacional. Em HTTP, todos
usam `shared#anonymous`: conexões, contextos e limites são compartilhados.
O serviço não oferece isolamento por usuário nem autenticação HTTP própria.

## Escopo suportado

PostgreSQL 18 e SQL Server 2022/2025 possuem adaptadores separados. Consulte
`capabilities` do contexto para a lista efetiva de recursos e funções.
JOINs, CTEs, agregações, filtros e janelas são aceitos somente quando analisáveis
pelo adaptador. SQL Server rejeita, entre outros, batches, hints, operações de
conjunto, recursão e janelas agregadas. Tipos/funções desconhecidos, views sem
suporte e parâmetros de execução anuláveis são rejeitados.

O SQL é preservado integralmente. Não há limite automático de linhas do resultado.
A nulabilidade pode permanecer desconhecida; equivalência de negócio e custo
não são comprovados. O consumidor deve autorizar e limitar qualquer execução.

## Orçamentos

SQL: 256 KiB; entrada: 512 KiB; saída: 1 MiB; AST: 100 mil nós e profundidade
128. O contexto expira em dez minutos. A análise isolada tem até sete segundos,
cada operação de metadados até cinco (incluindo conexão) e a requisição até vinte. Ultrapassar um
limite produz erro, nunca truncamento ou validação parcial.

Os testes usam dados sintéticos somente em `tests/`; eles não integram a imagem
nem representam um catálogo de produção. Veja [como verificar](testing.md).


## Operação

O processo emite eventos JSON em stderr por chamada, com ID aleatório de
correlação, ferramenta conhecida, duração e resultado (`success`, `rejected`,
`timeout` ou `internal_error`). Erros internos devolvem o mesmo ID ao cliente.
Não são registrados argumentos, SQL, nomes do banco, identidades, mensagens de
exceção ou credenciais. O coletor externo pode agregar latência, falhas e timeouts.

Continuações focadas por nomes exatos consultam somente as tabelas da página
solicitada, com revalidação de grants e fingerprints dos detalhes. Páginas amplas
continuam usando um índice vivo; não há cache que possa expor grants revogados.
A finalização mantém as duas verificações de metadados e privilégios, reutilizando
a extração determinística de dependências da mesma instrução SQL. A extração
de dependências usa a porta do analisador injetado, isolada em processo no produto.

Para clientes independentes, aplique o [isolamento por implantação](docker.md).

Contextos, cursores e fingerprints de detalhes são publicados juntos, somente
após validar o contrato e o tamanho da resposta. Respostas rejeitadas não deixam
handles não entregues ocupando as cotas. A expiração é conferida novamente antes
da publicação. Descoberta apenas por nomes não exige carregar tipos de colunas.
