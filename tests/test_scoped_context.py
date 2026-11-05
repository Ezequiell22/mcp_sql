"""Large live-schema-shaped indexes never become analyzer input or implicit scope."""

from dataclasses import asdict, replace

import pytest
from conftest import args_for

from atlas_sql_mcp.analysis.worker import MAX_WORKER_BYTES, ProcessAnalyzer
from atlas_sql_mcp.application.service import SqlService
from atlas_sql_mcp.domain import Catalog, Column, Principal, Relation, canonical


class ScopedCatalog:
    def __init__(self, relations):
        self.value = Catalog("large", 18, tuple(relations), "policy")
        self.loads = []

    def index(self, principal, connection_id):
        return replace(
            self.value,
            relations=tuple(
                replace(r, columns=tuple(replace(c, sql_type="") for c in r.columns))
                for r in self.value.relations
            ),
        )

    def load(self, principal, connection_id, relations=None):
        assert relations is not None, "Never load the complete typed database"
        self.loads.append(tuple(relations))
        return replace(
            self.value, relations=tuple(r for r in self.value.relations if r.key in relations)
        )


@pytest.fixture
def large():
    columns = (Column("id", "integer", False),) + tuple(
        Column(f"column_{i:02d}", "text", True) for i in range(40)
    )
    adapter = ScopedCatalog(Relation("erp", f"table_{i:04d}", columns) for i in range(1500))
    assert len(canonical(asdict(adapter.value))) > MAX_WORKER_BYTES
    return adapter, SqlService(adapter, ProcessAnalyzer()), Principal("local", "reader")


def context(service, principal, **options):
    return service.call(
        "atlas_sql_context_get",
        {
            "contractVersion": "1.2",
            "connectionId": "large",
            "request": "Order report",
            **options,
        },
        principal,
    )


def finalize(service, principal, context_id, table="erp.table_1499"):
    return service.call(
        "atlas_sql_finalize",
        {
            **args_for(f"SELECT id FROM {table}", ["id"], context_id),
            "contractVersion": "1.2",
        },
        principal,
    )


def test_large_index_is_names_only_and_cannot_authorize_finalization(large):
    adapter, service, principal = large
    result = context(service, principal)
    assert result["status"] == "context", result
    assert result["coverage"] == "partial"
    assert all(not r["columns"] for r in result["relations"])
    assert adapter.loads == []
    rejected = finalize(service, principal, result["contextId"])
    assert rejected["error"]["code"] == "CONTEXT_INCOMPLETE"


def test_report_only_loads_inspected_dependencies_despite_large_database(large):
    adapter, service, principal = large
    index = context(service, principal)
    focused = context(
        service, principal, contextId=index["contextId"], focus={"relations": ["erp.table_1499"]}
    )
    assert focused["status"] == "context", focused
    result = finalize(service, principal, focused["contextId"])
    assert result["status"] == "validated", result
    assert result["artifact"]["validation"]["executionPerformed"] is False
    assert adapter.loads and all(scope == ("erp.table_1499",) for scope in adapter.loads)


def test_selected_type_drift_invalidates_but_unrelated_type_drift_does_not(large):
    adapter, service, principal = large
    focused = context(service, principal, focus={"relations": ["erp.table_1499"]})
    first, *middle, last = adapter.value.relations
    adapter.value = replace(
        adapter.value,
        relations=(replace(first, columns=(Column("id", "bigint", False),)), *middle, last),
    )
    assert finalize(service, principal, focused["contextId"])["status"] == "validated"
    adapter.value = replace(
        adapter.value,
        relations=(
            *adapter.value.relations[:-1],
            replace(last, columns=(Column("id", "bigint", False),)),
        ),
    )
    assert finalize(service, principal, focused["contextId"])["error"]["code"] == "CONTEXT_STALE"


def test_index_cursor_cannot_be_reused_with_another_focus(large):
    _, service, principal = large
    first = context(service, principal, pageSize=100, focus={"include": []})
    next_page = context(
        service,
        principal,
        pageSize=100,
        contextId=first["contextId"],
        cursor=first["nextCursor"],
        focus={"include": []},
    )
    assert next_page["status"] == "context", next_page
    rejected = context(
        service,
        principal,
        pageSize=100,
        contextId=first["contextId"],
        cursor=first["nextCursor"],
        focus={"relations": ["erp.table_1499"]},
    )
    assert rejected["error"]["code"] == "CONTEXT_EXPIRED"


@pytest.mark.parametrize(
    "dialect,sql,expected",
    [
        (
            "postgresql",
            "WITH recent AS (SELECT id FROM erp.orders) SELECT id FROM recent",
            ["erp.orders"],
        ),
        ("sqlserver", "SELECT id FROM dbo.ORDR WHERE id > CAST(@p1 AS int)", ["dbo.ORDR"]),
    ],
)
def test_candidate_dependencies_use_isolated_dialect_parser(dialect, sql, expected):
    assert ProcessAnalyzer().references(sql, dialect) == expected


def test_overlapping_inspected_groups_preserve_scoped_foreign_key_fingerprints(catalog):
    class ForeignKeyScopedCatalog(ScopedCatalog):
        def load(self, principal, connection_id, relations=None):
            selected = super().load(principal, connection_id, relations)
            return replace(
                selected,
                relations=tuple(
                    replace(
                        relation,
                        constraints=tuple(
                            constraint
                            for constraint in relation.constraints
                            if constraint.kind != "f"
                            or f"{constraint.target_schema}.{constraint.target_relation}"
                            in relations
                        ),
                    )
                    for relation in selected.relations
                ),
            )

    adapter = ForeignKeyScopedCatalog(catalog.relations)
    service = SqlService(adapter, ProcessAnalyzer())
    principal = Principal("local", "reader")
    first = context(service, principal, focus={"relations": ["public.orders"]})
    assert first["status"] == "context", first
    assert all(
        relationship["kind"] != "f" for relationship in first["relations"][0]["relationships"]
    )
    second = context(
        service,
        principal,
        contextId=first["contextId"],
        focus={"relations": ["public.customers", "public.orders"]},
    )
    assert second["status"] == "context", second
    order_details = next(
        relation for relation in second["relations"] if relation["name"] == "orders"
    )
    assert any(relationship["kind"] == "f" for relationship in order_details["relationships"])
    result = service.call(
        "atlas_sql_finalize",
        {
            **args_for(
                "SELECT c.id AS customer_id, SUM(o.net_total) AS total "
                "FROM public.customers AS c JOIN public.orders AS o ON o.customer_id = c.id "
                "GROUP BY c.id",
                ["customer_id", "total"],
                first["contextId"],
            ),
            "contractVersion": "1.2",
        },
        principal,
    )
    assert result["status"] == "validated", result
    assert set(adapter.loads) == {
        ("public.orders",),
        ("public.customers", "public.orders"),
    }


def inherited_context():
    columns = (Column("id", "integer", False),)
    adapter = ScopedCatalog(
        [
            Relation("erp", "parent", columns, descendants=("erp.child",)),
            Relation("erp", "child", columns),
        ]
    )
    service = SqlService(adapter, ProcessAnalyzer())
    principal = Principal("local", "reader")
    parent = context(service, principal, focus={"relations": ["erp.parent"]})
    assert parent["status"] == "context", parent
    return adapter, service, principal, parent["contextId"]


def test_inherited_reads_require_inspected_child_metadata():
    adapter, service, principal, context_id = inherited_context()
    rejected = finalize(service, principal, context_id, table="erp.parent")
    assert rejected["error"]["code"] == "CONTEXT_INCOMPLETE"
    child = context(service, principal, contextId=context_id, focus={"relations": ["erp.child"]})
    assert child["status"] == "context", child
    result = finalize(service, principal, context_id, table="erp.parent")
    assert result["status"] == "validated", result
    assert result["artifact"]["dependencies"]["relations"] == ["erp.child", "erp.parent"]
    assert set(adapter.loads) == {("erp.parent",), ("erp.child",)}


def test_only_parent_read_does_not_require_child_inspection():
    _, service, principal, context_id = inherited_context()
    result = finalize(service, principal, context_id, table="ONLY erp.parent")
    assert result["status"] == "validated", result
    assert result["artifact"]["dependencies"]["relations"] == ["erp.parent"]


def test_oversized_failed_page_never_becomes_inspected_scope():
    relation = Relation(
        "erp", "wide", tuple(Column(f"c_{i}", "text", True, "x" * 4096) for i in range(200))
    )
    adapter = ScopedCatalog([relation])
    service = SqlService(adapter, ProcessAnalyzer())
    principal = Principal("local", "reader")
    failed = context(service, principal, focus={"relations": ["erp.wide"]})
    assert failed["error"]["code"] == "OUTPUT_TOO_LARGE", failed
    assert not service.state.contexts
    assert not service.state.cursors


def test_artifact_fingerprint_attests_typed_dependencies_not_names_only_index(large):
    adapter, service, principal = large
    focused = context(service, principal, focus={"relations": ["erp.table_1499"]})
    result = finalize(service, principal, focused["contextId"])
    assert result["status"] == "validated", result
    scoped = adapter.load(principal, "large", relations=("erp.table_1499",))
    assert result["artifact"]["dependencies"]["metadataFingerprint"] == scoped.fingerprint
    assert scoped.fingerprint != focused["metadataFingerprint"]


def test_dependency_extraction_folds_only_unquoted_postgres_identifiers():
    analyzer = ProcessAnalyzer()
    assert analyzer.references("SELECT ID FROM ERP.ORDERS", "postgresql") == ["erp.orders"]
    assert analyzer.references('SELECT "ID" FROM "ERP"."ORDERS"', "postgresql") == ["ERP.ORDERS"]


def test_focused_continuation_skips_full_index_and_uses_injected_dependencies(large):
    adapter, service, principal = large
    indexed = context(service, principal)

    def no_index(*args):
        raise AssertionError("Focused metadata must not rescan the complete database")

    adapter.index = no_index
    focused = context(
        service,
        principal,
        contextId=indexed["contextId"],
        focus={"relations": ["erp.table_1499"]},
    )
    assert focused["status"] == "context", focused
    assert adapter.loads == [("erp.table_1499",)]
    calls = []
    delegate = service.analyzer

    class InjectedAnalyzer:
        def dependencies(self, sql, dialect):
            calls.append((sql, dialect))
            return {"relations": ["erp.table_1499"], "inheritedRelations": []}

        def analyze(self, sql, catalog):
            return delegate.analyze(sql, catalog)

    service.analyzer = InjectedAnalyzer()
    result = finalize(service, principal, indexed["contextId"])
    assert result["status"] == "validated", result
    assert len(calls) == 1
    assert all(dialect == "postgresql" for _, dialect in calls)


def test_focused_cursor_reads_only_the_next_page_and_rechecks_authorization(large):
    from atlas_sql_mcp.domain import SqlError

    adapter, service, principal = large
    first = context(service, principal, focus={"include": []})
    names = ["erp.table_0000", "erp.table_1498", "erp.table_1499"]
    focus = {"relations": names}

    def no_index(*args):
        raise AssertionError("Exact-name pagination must not rediscover unrelated tables")

    adapter.index = no_index
    page = context(service, principal, contextId=first["contextId"], pageSize=1, focus=focus)
    assert page["status"] == "context", page
    assert [r["name"] for r in page["relations"]] == ["table_0000"]
    assert adapter.loads == [(names[0],)]
    second = context(
        service,
        principal,
        contextId=first["contextId"],
        pageSize=1,
        cursor=page["nextCursor"],
        focus=focus,
    )
    assert [r["name"] for r in second["relations"]] == ["table_1498"]
    assert adapter.loads == [(names[0],), (names[1],)]
    original = adapter.load

    def revoked(principal, key, relations=None):
        if names[2] in relations:
            raise SqlError("OBJECT_NOT_AUTHORIZED", "Grant revoked")
        return original(principal, key, relations)

    adapter.load = revoked
    denied = context(
        service,
        principal,
        contextId=first["contextId"],
        pageSize=1,
        cursor=second["nextCursor"],
        focus=focus,
    )
    assert denied["error"]["code"] == "OBJECT_NOT_AUTHORIZED"
    assert all(
        names[2] not in scope
        for scope in service.state.get(first["contextId"], principal).detail_groups
    )


def test_names_only_focus_does_not_require_typed_metadata(large):
    adapter, service, principal = large
    first = context(service, principal, focus={"include": []})
    names = context(
        service,
        principal,
        contextId=first["contextId"],
        focus={"relations": ["erp.table_1499"], "include": []},
    )
    assert names["status"] == "context", names
    assert [r["name"] for r in names["relations"]] == ["table_1499"]
    assert names["relations"][0]["columns"] == []
    assert not adapter.loads
    assert not service.state.get(first["contextId"], principal).detail_groups
