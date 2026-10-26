"""Compile Python syntax, verify schemas/docs, then run isolated regressions."""

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks-only", action="store_true", help="Skip regression tests")
    options = parser.parse_args()
    paths = sorted(
        path for folder in ("src", "tests", "scripts") for path in (ROOT / folder).rglob("*.py")
    )
    for path in paths:
        ast.parse(path.read_text(), filename=str(path))
    print(f"Python syntax: {len(paths)} files passed", flush=True)
    documents = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
    for document in documents:
        for target in re.findall(r"\[[^\]]*\]\(([^\s)#]+)(?:#[^)]*)?\)", document.read_text()):
            if "://" not in target and not (document.parent / unquote(target)).exists():
                raise SystemExit(f"Broken local documentation link in {document.name}: {target}")
    print("Local documentation links: passed", flush=True)
    commands = [["-m", "atlas_sql_mcp.contracts", "schemas", "--check"]]
    if not options.checks_only:
        commands.append(["-m", "pytest"])
    for args in commands:
        subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
