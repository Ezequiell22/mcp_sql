"""Offline, bounded syntax diagnostic. This command never issues an SQL artifact."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .analyzer import MAX_DEPTH, MAX_NODES, MAX_SQL_BYTES
from .domain import SqlError


def parse_only(sql: str, dialect: str) -> dict:
    if not sql.strip() or len(sql.encode()) > MAX_SQL_BYTES:
        raise SqlError("INPUT_TOO_LARGE", "Provide nonempty SQL within 256 KiB.", "sql")
    if dialect == "postgresql":
        from pglast import parser

        try:
            tree = json.loads(parser.parse_sql_json(sql))
        except (parser.ParseError, ValueError):
            raise SqlError(
                "SQL_PARSE_ERROR", "PostgreSQL parser rejected the SQL.", "sql"
            ) from None
        if len(tree["stmts"]) != 1:
            raise SqlError("SQL_PARSE_ERROR", "Provide exactly one statement.", "sql")
        stack = [(tree, 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                raise SqlError("INPUT_TOO_LARGE", "SQL exceeds the AST budget.", "sql")
            values = (
                node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
            )
            stack.extend((value, depth + 1) for value in values)
        evidence = "native_parser"
    elif dialect == "sqlserver":
        import sqlglot
        from sqlglot.errors import ErrorLevel, SqlglotError

        try:
            parsed = sqlglot.parse(sql, read="tsql", error_level=ErrorLevel.RAISE)
        except SqlglotError:
            raise SqlError("SQL_PARSE_ERROR", "T-SQL parser rejected the SQL.", "sql") from None
        if len(parsed) != 1 or parsed[0] is None:
            raise SqlError("SQL_PARSE_ERROR", "Provide exactly one statement.", "sql")
        stack = [(parsed[0], 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                raise SqlError("INPUT_TOO_LARGE", "SQL exceeds the AST budget.", "sql")
            stack.extend((child, depth + 1) for child in node.iter_expressions())
        evidence = "non_native_parser_only"
    else:
        raise SqlError("UNSUPPORTED_DIALECT", "No syntax adapter exists for this dialect.")
    return {
        "status": "parsed",
        "dialect": dialect,
        "evidence": evidence,
        "executionPerformed": False,
        "limitations": "Syntax diagnostic only. Authorization, read-only policy, types, bindings, business meaning and execution cost are not checked. T-SQL requires native engine conformance before release.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline SQL syntax diagnostic; never executes SQL"
    )
    parser.add_argument("sql_file", type=Path, nargs="?")
    parser.add_argument("--dialect", choices=["postgresql", "sqlserver"], required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
            if sys.platform.startswith("linux"):
                resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            result = parse_only(sys.stdin.read(MAX_SQL_BYTES + 1), args.dialect)
        except SqlError as error:
            result = error.envelope()
        except Exception:
            result = SqlError("SQL_PARSE_ERROR", "Syntax diagnostic did not complete.").envelope()
        print(json.dumps(result))
        return
    if args.sql_file is None:
        parser.error("sql_file is required")
    try:
        with args.sql_file.open("rb") as stream:
            data = stream.read(MAX_SQL_BYTES + 1)
        if len(data) > MAX_SQL_BYTES:
            raise SqlError("INPUT_TOO_LARGE", "SQL exceeds 256 KiB.")
        data.decode("utf-8")
        completed = subprocess.run(
            [sys.executable, "-m", "atlas_sql_mcp.check", "--worker", "--dialect", args.dialect],
            input=data,
            capture_output=True,
            timeout=5,
            env={k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "PYTHONPATH"}},
            check=False,
        )
        if completed.returncode or len(completed.stdout) > 8192:
            raise SqlError("SQL_PARSE_ERROR", "Syntax diagnostic exceeded its resource budget.")
        result = json.loads(completed.stdout)
    except SqlError as error:
        result = error.envelope()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result = SqlError(
            "SQL_PARSE_ERROR", "Could not read or parse SQL within the diagnostic budget."
        ).envelope()
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result["status"] == "error" else 0)


if __name__ == "__main__":
    main()
