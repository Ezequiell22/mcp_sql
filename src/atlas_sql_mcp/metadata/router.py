"""Select a database adapter without coupling one dialect to another."""

from pathlib import Path

from atlas_sql_mcp.config import load_connections
from atlas_sql_mcp.domain import Catalog, Principal, SqlError
from atlas_sql_mcp.metadata.postgres import PostgresCatalog
from atlas_sql_mcp.metadata.sqlserver import SqlServerCatalog


class DialectCatalog:
    """Reload policy on each call; choose an adapter from operator configuration."""

    def __init__(self, connections: Path | dict):
        self.connections = connections

    def _config(self, principal: Principal, connection_id: str):
        try:
            configs = (
                load_connections(self.connections)
                if isinstance(self.connections, Path)
                else self.connections
            )
        except (ValueError, OSError):
            raise SqlError(
                "CONNECTION_POLICY_UNAVAILABLE", "Connection policy could not be loaded."
            ) from None
        config = configs.get(connection_id)
        if not config:
            raise SqlError(
                "CONNECTION_NOT_AUTHORIZED", "Connection is unavailable for this identity."
            )
        config.authorized(principal)
        return configs, config

    def load(self, principal: Principal, connection_id: str, relations=None) -> Catalog:
        configs, config = self._config(principal, connection_id)
        adapter = SqlServerCatalog if config.dialect == "sqlserver" else PostgresCatalog
        return adapter(configs).load(principal, connection_id, relations=relations)

    def index(self, principal: Principal, connection_id: str) -> Catalog:
        from atlas_sql_mcp.domain import Column, Relation
        from atlas_sql_mcp.metadata.discovery import discover

        _, config = self._config(principal, connection_id)
        grants = config.authorized(principal)
        rows = discover(config, sorted({key.split(".")[0] for key in grants}))
        current = {}
        for schema, table, column in rows:
            key = f"{schema}.{table}"
            if column in grants.get(key, ()):
                current.setdefault(key, []).append(Column(column, "", True))
        entries = tuple(
            Relation(*key.split("."), tuple(cols)) for key, cols in sorted(current.items())
        )
        return Catalog(
            connection_id,
            getattr(rows, "database_major", None) if config.dialect == "sqlserver" else 18,
            entries,
            config.policy_version,
            config.dialect,
        )
