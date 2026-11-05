"""Disposable parser process: bounds native-parser crashes, CPU time and cancellation."""

import json
import os
import subprocess
import sys
from dataclasses import asdict

from atlas_sql_mcp.analysis.limits import MAX_DEPTH, MAX_NODES, MAX_SQL_BYTES
from atlas_sql_mcp.budget import remaining
from atlas_sql_mcp.domain import Catalog, Column, Constraint, Relation, SqlError


def catalog_from_dict(raw: dict) -> Catalog:
    relations = []
    for value in raw["relations"]:
        value["columns"] = tuple(Column(**c) for c in value["columns"])
        value["constraints"] = tuple(
            Constraint(
                **{
                    **c,
                    "columns": tuple(c["columns"]),
                    "target_columns": tuple(c["target_columns"]),
                }
            )
            for c in value["constraints"]
        )
        value["descendants"] = tuple(value["descendants"])
        relations.append(Relation(**value))
    return Catalog(
        raw["connection_id"],
        raw["postgres_major"],
        tuple(relations),
        raw["policy_version"],
        raw.get("dialect", "postgresql"),
        raw.get("database_major"),
    )


MAX_WORKER_BYTES = 2 * 1024 * 1024


class ProcessAnalyzer:
    def __init__(self, seconds: int = 7):
        self.seconds = seconds

    def _run(self, payload: dict) -> dict:
        encoded = json.dumps(payload)
        if len(encoded.encode()) > MAX_WORKER_BYTES:
            raise SqlError(
                "INPUT_TOO_LARGE",
                "Selected SQL metadata exceeds the isolated analysis budget. Request a narrower report scope.",
            )
        # Parser receives no connection configuration, tokens or credential variables.
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "PYTHONPATH"}}
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "atlas_sql_mcp.analysis.worker"],
                input=encoded,
                capture_output=True,
                text=True,
                timeout=remaining(self.seconds),
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SqlError(
                "VALIDATION_TIMEOUT", "SQL analysis exceeded its time budget.", retryable=True
            ) from None
        if completed.returncode != 0 or len(completed.stdout.encode()) > 1024 * 1024:
            raise SqlError(
                "UNSUPPORTED_SQL_FEATURE", "SQL analysis exceeded its isolated resource budget."
            )
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            raise SqlError(
                "UNSUPPORTED_SQL_FEATURE", "SQL analyzer could not return a complete result."
            ) from None
        if "error" in result:
            e = result["error"]
            raise SqlError(e["code"], e["message"], e["fieldPath"], e["retryable"])
        return result

    def dependencies(self, sql: str, dialect: str) -> dict:
        return self._run({"operation": "references", "sql": sql, "dialect": dialect})

    def references(self, sql: str, dialect: str) -> list[str]:
        return self.dependencies(sql, dialect)["relations"]

    def analyze(self, sql: str, catalog: Catalog) -> dict:
        result = self._run({"sql": sql, "catalog": asdict(catalog)})
        result["parameters"] = {int(k): v for k, v in result["parameters"].items()}
        result["parameterPredicates"] = {
            int(k): v for k, v in result["parameterPredicates"].items()
        }
        result["parameterExpressions"] = {
            int(k): v for k, v in result["parameterExpressions"].items()
        }
        result["parameterRanges"] = {int(k): v for k, v in result["parameterRanges"].items()}
        return result


def references(sql: str, dialect: str) -> dict:
    """Find candidate dependencies in isolation; full analysis remains authoritative."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel, ParseError

    if dialect not in {"postgresql", "sqlserver"}:
        raise SqlError("UNSUPPORTED_DIALECT", "No parser is configured for this dialect.")
    if len(sql.encode()) > MAX_SQL_BYTES:
        raise SqlError("INPUT_TOO_LARGE", "SQL exceeds its analysis byte budget.")
    try:
        statements = sqlglot.parse(
            sql, read="tsql" if dialect == "sqlserver" else "postgres", error_level=ErrorLevel.RAISE
        )
    except (ParseError, RecursionError):
        raise SqlError("SQL_PARSE_ERROR", "The SQL parser rejected the statement.", "sql") from None
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
        raise SqlError(
            "READ_ONLY_POLICY_VIOLATION", "Supply one read-only SELECT statement.", "sql"
        )
    root = statements[0]
    if dialect == "postgresql":
        from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

        root = normalize_identifiers(root, dialect="postgres")
    count = 0
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise SqlError("INPUT_TOO_LARGE", "SQL exceeds its structural analysis budget.")
        stack.extend((child, depth + 1) for child in node.iter_expressions())
    # CTE names are unqualified and deliberately omitted. Any unqualified base
    # table, cross-database reference or unsupported node is rejected by the full
    # dialect analyzer; dependency extraction grants no permissions itself.
    tables = [t for t in root.find_all(exp.Table) if t.db]
    return {
        "relations": sorted({f"{t.db}.{t.name}" for t in tables}),
        "inheritedRelations": sorted(
            {f"{t.db}.{t.name}" for t in tables if not t.args.get("only")}
        ),
    }


def main() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
        # macOS does not consistently support address-space limits. Input/AST bounds
        # and process deadlines apply there; Linux additionally limits address space.
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        encoded = sys.stdin.buffer.read(MAX_WORKER_BYTES + 1)
        if len(encoded) > MAX_WORKER_BYTES:
            raise SqlError(
                "INPUT_TOO_LARGE", "Selected SQL metadata exceeds the isolated analysis budget."
            )
        raw = json.loads(encoded)
        if raw.get("operation") == "references":
            sys.stdout.write(json.dumps(references(raw["sql"], raw["dialect"])))
            return
        catalog = catalog_from_dict(raw["catalog"])
        if catalog.dialect == "sqlserver":
            from atlas_sql_mcp.analysis.sqlserver import SqlServerAnalyzer

            analyzer = SqlServerAnalyzer()
        elif catalog.dialect == "postgresql":
            from atlas_sql_mcp.analysis.postgres import PostgresAnalyzer

            analyzer = PostgresAnalyzer()
        else:
            raise SqlError("UNSUPPORTED_DIALECT", "No analyzer is configured for this dialect.")
        result = analyzer.analyze(raw["sql"], catalog)
    except SqlError as error:
        result = error.envelope()
    except Exception:
        result = SqlError(
            "UNSUPPORTED_SQL_FEATURE", "Isolated analysis could not establish a safe result."
        ).envelope()
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
