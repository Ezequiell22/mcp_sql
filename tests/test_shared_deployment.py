"""Real Docker HTTPS ingress/JWKS → authenticated MCP → verified SQL Server TLS."""

import json
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_sqlserver_live import docker
from test_sqlserver_live import live_sqlserver as live_sqlserver
from test_sqlserver_live import pytestmark as pytestmark

PROXY = """
import http.client, http.server, json, ssl
from pathlib import Path
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self, *args): pass
 def do_GET(self): self.handle_request()
 def do_POST(self): self.handle_request()
 def handle_request(self):
  if self.path == '/jwks':
   data = Path('/fixture/jwks.json').read_bytes()
   self.send_response(200); self.send_header('Content-Type','application/json')
   self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
   return
  body = self.rfile.read(int(self.headers.get('Content-Length','0')))
  conn = http.client.HTTPConnection('mcp-shared',8091,timeout=30)
  conn.request(self.command,self.path,body,dict(self.headers))
  response=conn.getresponse(); data=response.read()
  self.send_response(response.status)
  for key,value in response.getheaders():
   if key.lower() not in {'transfer-encoding','connection','content-length'}: self.send_header(key,value)
  self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
  conn.close()
server=http.server.ThreadingHTTPServer(('0.0.0.0',8443),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/fixture/ingress.pem','/fixture/ingress.key')
server.socket=context.wrap_socket(server.socket,server_side=True)
server.serve_forever()
"""
CLIENT = """
import asyncio, json
from pathlib import Path
import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
async def run():
 config=json.loads(Path('/fixture/client.json').read_text())
 async with httpx.AsyncClient(verify='/fixture/server.pem',timeout=30) as client:
  for _ in range(40):
   try:
    if (await client.get('https://reports:8443/healthz')).status_code==200: break
   except httpx.HTTPError: pass
   await asyncio.sleep(.25)
  else: raise RuntimeError('Isolated HTTPS ingress did not become ready')
  assert (await client.post('https://reports:8443/mcp',json={})).status_code==401
  metadata=await client.get('https://reports:8443/.well-known/oauth-protected-resource/mcp')
  assert metadata.status_code==200 and metadata.json()['resource']=='https://reports:8443/mcp'
  client.headers['Authorization']='Bearer '+config['token']
  rejected=await client.post('https://reports:8443/mcp',json={},headers={'Origin':'https://untrusted.test'})
  assert rejected.status_code in (400,403,421)
  async with streamable_http_client('https://reports:8443/mcp',http_client=client) as (read,write):
   async with ClientSession(read,write) as session:
    await session.initialize()
    connected=(await session.call_tool('atlas_sql_connect',{'connectionString':config['connectionString']})).structured_content
    assert connected['status']=='connected', connected
    context=(await session.call_tool('atlas_sql_context_get',{'contractVersion':'1.2','connectionId':connected['connectionId'],'request':'Invoice totals by region'})).structured_content
    assert context['status']=='context', context
    query='SELECT a.region, SUM(i.amount) AS total FROM dbo.invoices i JOIN dbo.accounts a ON a.id=i.account_id GROUP BY a.region'
    proposal={'contractVersion':'1.2','contextId':context['contextId'],'sql':query,'parameters':[],'filters':[],'outputs':[{'key':'region','label':'Region'},{'key':'total','label':'Total'}],'semantics':{'grain':['region'],'countedEntities':[],'measures':[],'assumptions':[],'resolvedDecisions':[]}}
    result=(await session.call_tool('atlas_sql_finalize',proposal)).structured_content
    assert result['status']=='validated' and result['artifact']['sql']==query, result
    assert result['artifact']['validation']['executionPerformed'] is False
    client.headers['Authorization']='Bearer '+config['otherToken']
    denied=(await session.call_tool('atlas_sql_context_get',{'contractVersion':'1.2','connectionId':connected['connectionId'],'request':'Invoice totals'})).structured_content
    assert denied['error']['code']=='CONNECTION_NOT_AUTHORIZED',denied
 print(json.dumps({'authenticatedDeployment':'passed','crossIdentityDenied':True}))
asyncio.run(run())
"""


def test_real_shared_http_deployment(live_sqlserver):
    fixture = live_sqlserver
    folder = fixture["folder"]
    folder.chmod(0o755)  # Allow the unprivileged disposable containers to read test-only files.
    suffix = secrets.token_hex(6)
    backend, ingress = f"atlas-http-{suffix}", f"atlas-ingress-{suffix}"
    issuer, resource = "https://issuer:8443", "https://reports:8443/mcp"
    now = datetime.now(UTC)
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key()))
    jwk.update(kid="fixture", use="sig", alg="RS256")
    (folder / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    ca = x509.load_pem_x509_certificate((folder / "server.pem").read_bytes())
    ca_key = serialization.load_pem_private_key((folder / "server.key").read_bytes(), password=None)
    ingress_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "issuer")]))
        .issuer_name(ca.subject)
        .public_key(ingress_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("issuer"), x509.DNSName("reports")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    (folder / "ingress.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (folder / "ingress.key").write_bytes(
        ingress_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    def token(subject):
        return jwt.encode(
            {
                "iss": issuer,
                "aud": resource,
                "sub": subject,
                "scope": "atlas:sql",
                "exp": now + timedelta(minutes=5),
            },
            signing,
            algorithm="RS256",
            headers={"kid": "fixture"},
        )

    (folder / "client.json").write_text(
        json.dumps(
            {
                "token": token("report-user"),
                "otherToken": token("other-user"),
                "connectionString": "Server=erp-sql;Database=erp;UID=erp_reader;PWD="
                + fixture["password"],
            }
        )
    )
    (folder / "proxy.py").write_text(PROXY)
    (folder / "client.py").write_text(CLIENT)
    mount = f"type=bind,source={folder},target=/fixture,readonly"
    common = [
        "--network",
        fixture["network"],
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,size=128m",
        "--mount",
        mount,
        "--env",
        "SSL_CERT_FILE=/fixture/server.pem",
    ]
    try:
        docker(
            "run",
            "-d",
            "--name",
            backend,
            "--network-alias",
            "mcp-shared",
            *common,
            "--env",
            f"ATLAS_SQL_ISSUER={issuer}",
            "--env",
            f"ATLAS_SQL_JWKS_URL={issuer}/jwks",
            "--env",
            f"ATLAS_SQL_PUBLIC_URL={resource}",
            "--env",
            "ATLAS_SQL_CONNECTION_HOSTS=erp-sql:1433",
            fixture["image"],
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            check=True,
        )
        docker(
            "run",
            "-d",
            "--name",
            ingress,
            "--network-alias",
            "issuer",
            "--network-alias",
            "reports",
            *common,
            "--entrypoint",
            "python",
            fixture["image"],
            "/fixture/proxy.py",
            check=True,
        )
        result = docker(
            "run", "--rm", *common, "--entrypoint", "python", fixture["image"], "/fixture/client.py"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["authenticatedDeployment"] == "passed"
    finally:
        docker("rm", "--force", ingress)
        docker("rm", "--force", backend)
