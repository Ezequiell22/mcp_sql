"""Immutable catalog models and application ports, independent of transports/drivers."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Protocol


class SqlError(Exception):
    def __init__(self, code: str, message: str, field: str = "", retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.field, self.retryable = code, message, field, retryable

    def envelope(self) -> dict:
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "fieldPath": self.field,
                "retryable": self.retryable,
            },
        }


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str

    @property
    def key(self) -> str:
        return f"{self.issuer}#{self.subject}"


@dataclass(frozen=True)
class Column:
    name: str
    postgres_type: str
    nullable: bool
    description: str = ""
    type_schema: str = "pg_catalog"


@dataclass(frozen=True)
class Constraint:
    name: str
    kind: str
    columns: tuple[str, ...]
    target_schema: str = ""
    target_relation: str = ""
    target_columns: tuple[str, ...] = ()
    validated: bool = True
    enforced: bool = True


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str
    columns: tuple[Column, ...]
    constraints: tuple[Constraint, ...] = ()
    description: str = ""
    kind: str = "table"
    descendants: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def column_map(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}


@dataclass(frozen=True)
class Catalog:
    connection_id: str
    postgres_major: int
    relations: tuple[Relation, ...]
    policy_version: str
    dialect: str = "postgresql"
    database_major: int | None = None

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))

    @property
    def relation_map(self) -> dict[str, Relation]:
        return {r.key: r for r in self.relations}


class CatalogPort(Protocol):
    def load(self, principal: Principal, connection_id: str) -> Catalog: ...


class AnalyzerPort(Protocol):
    def analyze(self, sql: str, catalog: Catalog) -> dict: ...
