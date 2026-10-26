"""Shared transport authentication; identity never comes from tool arguments."""

import anyio
import jwt
from mcp.server.auth.provider import AccessToken

from .config import validate_public_url


class JwtVerifier:
    def __init__(self, issuer: str, jwks_url: str, resource: str):
        self.issuer = validate_public_url(issuer)
        self.resource = validate_public_url(resource)
        self.jwks = jwt.PyJWKClient(
            validate_public_url(jwks_url), cache_keys=False, lifespan=300, timeout=3
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > 16384:
            return None
        return await anyio.to_thread.run_sync(self._verify, token)

    def _verify(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.resource,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
            if (
                not isinstance(claims["sub"], str)
                or not claims["sub"]
                or not isinstance(claims.get("scope", ""), str)
            ):
                return None
            return AccessToken(
                token=token,
                client_id=str(claims.get("azp", claims["sub"])),
                subject=claims["sub"],
                scopes=claims.get("scope", "").split(),
                expires_at=int(claims["exp"]),
                resource=self.resource,
                claims=claims,
            )
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            return None
