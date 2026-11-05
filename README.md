# Atlas SQL MCP

Conecta um agente de IA ao PostgreSQL 18 ou SQL Server 2022/2025 para descobrir
metadados e validar SQL. O agente escreve a consulta; o MCP faz a validação
estática e devolve o SQL completo, filtros tipados e evidências.

**Não executa consultas geradas, não lê registros de negócio e não possui LLM
próprio.** Não exige catálogo fictício, dados de demonstração ou aplicação de relatórios.

## Como usar

1. O administrador instala o servidor e disponibiliza o endpoint HTTPS `/mcp`.
2. Configure seu cliente com um dos [modelos de conexão](configs/README.md).
3. Informe uma connection string de um usuário restrito à leitura e descreva o
   resultado desejado. O agente descobre as tabelas reais, esclarece regras de
   negócio e submete a consulta à validação.

As credenciais ficam na memória do servidor por até uma hora; reiniciar encerra
as sessões. O cliente de IA pode registrar a conversa e os argumentos das
ferramentas: siga a política de credenciais da sua organização.

## Instalação

Com Docker, após aceitar a licença do driver Microsoft:

```sh
docker build --build-arg ACCEPT_EULA=Y -t atlas-sql-mcp:local .
```

Continue em [implantação com Docker](docs/docker.md). Para executar com Python
3.12 ou superior, veja [configuração](docs/configuration.md).

## Documentação

| Assunto | Guia |
| --- | --- |
| Instalação, connection strings e permissões | [Configuração](docs/configuration.md) |
| Contêiner, certificados e serviço HTTP | [Docker](docs/docker.md) |
| Codex, Cursor e Claude Code | [Clientes](configs/README.md) |
| Componentes, segurança e limites | [Arquitetura](docs/architecture.md) |
| Ferramentas, parâmetros e versões | [Contrato](docs/contract.md) |
| Verificação local e integração | [Testes](docs/testing.md) |

A validação cobre apenas o subconjunto anunciado nas capacidades da conexão.
Não comprova regras de negócio, desempenho ou sucesso de execução. O HTTP não
possui autenticação própria e compartilha a identidade interna entre os clientes;
o controle de acesso deve ser aplicado pela infraestrutura externa.
