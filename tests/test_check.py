import json
import subprocess
import sys

import pytest

from atlas_sql_mcp.check import parse_only
from atlas_sql_mcp.domain import SqlError


@pytest.mark.parametrize("dialect", ["postgresql", "sqlserver"])
def test_offline_syntax_is_not_validation(dialect):
    result = parse_only("SELECT 1", dialect)
    assert result["status"] == "parsed"
    assert "validated" not in json.dumps(result)
    assert result["executionPerformed"] is False
    assert "Authorization" in result["limitations"]
    with pytest.raises(SqlError):
        parse_only("SELECT 1; SELECT 2", dialect)
    with pytest.raises(SqlError):
        parse_only("SELECT (", dialect)


@pytest.mark.parametrize("dialect", ["postgresql", "sqlserver"])
def test_offline_cli_in_isolated_process(tmp_path, dialect):
    path = tmp_path / "statement.sql"
    path.write_text("SELECT 1")
    result = subprocess.run(
        [sys.executable, "-m", "atlas_sql_mcp.check", "--dialect", dialect, str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "parsed"
    path.write_text("SELECT 1; SELECT 2")
    result = subprocess.run(
        [sys.executable, "-m", "atlas_sql_mcp.check", "--dialect", dialect, str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "error"
