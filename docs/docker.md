# Docker

A imagem inclui os drivers PostgreSQL e Microsoft ODBC 18, executa como UID 10001
e não inclui testes, dados sintéticos ou credenciais. O build exige aceitação da
licença Microsoft:

```sh
docker build --build-arg ACCEPT_EULA=Y -t atlas-sql-mcp:local .
```

## Stdio local

Configure o cliente para executar `docker` com estes argumentos:

```json
["run", "--rm", "-i", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--cap-drop=ALL", "--security-opt=no-new-privileges", "atlas-sql-mcp:local"]
```

Mantenha `-i` e não use `-t`. Não é necessário publicar porta. Para um contêiner
persistente, execute `ACCEPT_EULA=Y docker compose up -d --build` e registre o
cliente com `docker` e os argumentos
`["exec", "-i", "atlas-sql-mcp", "atlas-sql-mcp"]`. Cada cliente cria um processo
MCP próprio; seus handles expiram quando o processo termina.

`localhost` indica o próprio contêiner. Use o hostname real do banco, uma rede
Docker compartilhada ou `host.docker.internal` no Docker Desktop. Em Linux,
o acesso ao host pode exigir `--add-host=host.docker.internal:host-gateway`.
Hostnames de contêiner são destinos remotos e exigem TLS verificado.

## HTTP compartilhado

Crie um arquivo privado de ambiente com `ATLAS_SQL_PUBLIC_URL` e
`ATLAS_SQL_CONNECTION_HOSTS`, conforme [configuração](configuration.md).
Disponibilize ingresso HTTPS e controle de acesso externo; depois execute:

```sh
ACCEPT_EULA=Y ATLAS_SQL_HTTP_ENV_FILE=/caminho/privado/atlas-http.env \
  docker compose -f compose.yaml -f configs/compose.http.yaml up -d --build
```

O backend fica em `127.0.0.1:8091`. O ingresso precisa preservar o Host público.
Registre a URL HTTPS `/mcp` nos [clientes](../configs/README.md).
O MCP não autentica clientes: todos compartilham `shared#anonymous`, conexões e
contextos. `/healthz` informa somente se o processo responde, não a disponibilidade
do banco. Reinícios apagam as sessões; múltiplas réplicas exigem afinidade de sessão.

## Certificados e credenciais

Para CA privada na imagem Linux, monte um bundle PEM confiável em modo somente
leitura, acessível ao UID 10001. Configure `PGSSLROOTCERT` e `SSL_CERT_FILE` com
o caminho **dentro do contêiner**. O certificado deve corresponder ao hostname
do banco; não desative a verificação. Inclua as raízes necessárias no bundle.

Não coloque connection strings em argumentos de build, imagem ou arquivos
versionados. Arquivos de conexões administradas podem ser montados somente para
leitura e passados por `--connections`; a identidade stdio da imagem é `local#atlas`.

Para parar o serviço, use `docker compose down` com os mesmos arquivos Compose.
Valide a imagem no ambiente de destino e registre seu digest antes de implantar;
um build local não certifica compatibilidade ou segurança de produção.
