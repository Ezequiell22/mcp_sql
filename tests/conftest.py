from copy import deepcopy

import pytest

from atlas_sql_mcp.analyzer import PostgresAnalyzer
from atlas_sql_mcp.domain import Catalog, Column, Constraint, Principal, Relation
from atlas_sql_mcp.service import SqlService


@pytest.fixture
def catalog():
    return Catalog(
        "sales",
        18,
        (
            Relation(
                "public",
                "customers",
                (
                    Column("id", "integer", False),
                    Column("name", "text", False),
                    Column("region", "text", True),
                ),
                (Constraint("customers_pk", "p", ("id",)),),
            ),
            Relation(
                "public",
                "orders",
                (
                    Column("id", "integer", False),
                    Column("customer_id", "integer", False),
                    Column("ordered_on", "date", False),
                    Column("net_total", "numeric(20,2)", False),
                    Column("status", "text", False),
                    Column("created_at", "timestamp with time zone", False),
                    Column("quantity", "bigint", False),
                    Column("active", "boolean", False),
                    Column("payload", "jsonb", True),
                    Column("tags", "text[]", True),
                ),
                (
                    Constraint("orders_pk", "p", ("id",)),
                    Constraint(
                        "orders_customer_fk", "f", ("customer_id",), "public", "customers", ("id",)
                    ),
                ),
            ),
            Relation(
                "public",
                "items",
                (
                    Column("id", "integer", False),
                    Column("order_id", "integer", False),
                    Column("amount", "numeric(20,2)", False),
                    Column("quantity", "integer", False),
                ),
                (
                    Constraint("items_pk", "p", ("id",)),
                    Constraint("items_order_fk", "f", ("order_id",), "public", "orders", ("id",)),
                ),
            ),
            Relation(
                "public",
                "composite",
                (
                    Column("tenant_id", "integer", False),
                    Column("id", "integer", False),
                    Column("label", "text", False),
                ),
                (Constraint("composite_pk", "p", ("tenant_id", "id")),),
            ),
        ),
        "fixture-policy",
    )


class MemoryCatalog:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def load(self, principal, connection_id):
        self.calls.append((principal, connection_id))
        from atlas_sql_mcp.domain import SqlError

        if connection_id != self.value.connection_id:
            raise SqlError("CONNECTION_NOT_AUTHORIZED", "Unavailable.")
        return self.value


@pytest.fixture
def principal():
    return Principal("local", "analyst")


@pytest.fixture
def service(catalog):
    return SqlService(MemoryCatalog(catalog), PostgresAnalyzer())


def args_for(sql, outputs, context_id="test"):
    return {
        "contractVersion": "1.0",
        "contextId": context_id,
        "sql": sql,
        "parameters": [],
        "filters": [],
        "outputs": [{"key": key, "label": key} for key in outputs],
        "semantics": {
            "grain": [],
            "countedEntities": [],
            "measures": [],
            "assumptions": [],
            "resolvedDecisions": [],
        },
    }


def context_for(service, principal, **kwargs):
    return service.call(
        "atlas_sql_context_get",
        {
            "contractVersion": "1.0",
            "connectionId": "sales",
            "request": "Receita dos pedidos",
            **kwargs,
        },
        principal,
    )


def with_range(args):
    args = deepcopy(args)
    args["parameters"] = [
        {
            "position": i,
            "name": name,
            "postgresType": "date",
            "nullable": False,
            "binding": {"filterId": "period", "field": field},
        }
        for i, name, field in [(1, "period_start", "from"), (2, "period_until", "until")]
    ]
    args["filters"] = [
        {
            "id": "period",
            "label": "Período",
            "kind": "date_range",
            "required": True,
            "fields": {
                name: {
                    "type": "date",
                    "nullable": False,
                    "inclusive": inclusive,
                    "parameterPosition": pos,
                }
                for name, inclusive, pos in [("from", True, 1), ("until", False, 2)]
            },
            "validation": {"ordering": "from < until"},
        }
    ]
    return args
