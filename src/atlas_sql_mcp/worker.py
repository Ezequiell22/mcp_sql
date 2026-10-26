"""Disposable parser process: bounds native-parser crashes, CPU time and cancellation."""

import json
import os
import subprocess
import sys
from dataclasses import asdict

from .budget import remaining
from .domain import Catalog, Column, Constraint, Relation, SqlError


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
        raw["connection_id"], raw["postgres_major"], tuple(relations), raw["policy_version"]
    )


class ProcessAnalyzer:
    def __init__(self, seconds: int = 7):
        self.seconds = seconds

    def analyze(self, sql: str, catalog: Catalog) -> dict:
        # Parser receives no connection configuration, tokens or credential variables.
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "PYTHONPATH"}}
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "atlas_sql_mcp.worker"],
                input=json.dumps({"sql": sql, "catalog": asdict(catalog)}),
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
        result["parameters"] = {int(k): v for k, v in result["parameters"].items()}
        result["parameterPredicates"] = {
            int(k): v for k, v in result["parameterPredicates"].items()
        }
        result["parameterExpressions"] = {
            int(k): v for k, v in result["parameterExpressions"].items()
        }
        result["parameterRanges"] = {int(k): v for k, v in result["parameterRanges"].items()}
        return result


def main() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
        # macOS does not consistently support address-space limits. Input/AST bounds
        # and process deadlines apply there; Linux additionally limits address space.
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        from .analyzer import PostgresAnalyzer

        raw = json.loads(sys.stdin.read(2 * 1024 * 1024))
        result = PostgresAnalyzer().analyze(raw["sql"], catalog_from_dict(raw["catalog"]))
    except SqlError as error:
        result = error.envelope()
    except Exception:
        result = SqlError(
            "UNSUPPORTED_SQL_FEATURE", "Isolated analysis could not establish a safe result."
        ).envelope()
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
