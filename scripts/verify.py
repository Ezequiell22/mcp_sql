"""Verify source, docs, examples, schemas and regression checks from one entry point."""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def check_documents():
    documents = [ROOT / "README.md", ROOT / "AGENTS.md"]
    documents += [
        p
        for folder in ("docs", "configs", "examples", "labs")
        for p in (ROOT / folder).rglob("*.md")
    ]
    for document in documents:
        for raw in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", document.read_text()):
            url = urlsplit(raw)
            if url.scheme or url.netloc:
                continue
            target = (document.parent / unquote(url.path)) if url.path else document
            if not target.exists():
                raise SystemExit(f"Broken link in {document.relative_to(ROOT)}: {raw}")
            if url.fragment and target.suffix == ".md":
                headings = re.findall(r"^#{1,6}\s+(.+?)\s*#*$", target.read_text(), re.MULTILINE)
                anchors = {re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-") for h in headings}
                if unquote(url.fragment) not in anchors:
                    raise SystemExit(f"Broken heading in {document.relative_to(ROOT)}: {raw}")
    print("Documentation links and headings: passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks-only", action="store_true", help="Skip regression tests")
    parser.add_argument(
        "--integration",
        action="store_true",
        help="Require all integration gates and zero skipped tests",
    )
    options = parser.parse_args()
    if options.checks_only and options.integration:
        parser.error("--checks-only and --integration are mutually exclusive")
    paths = [
        p for folder in ("src", "tests", "scripts", "labs") for p in (ROOT / folder).rglob("*.py")
    ]
    for path in paths:
        ast.parse(path.read_text(), filename=str(path))
    print(f"Python syntax: {len(paths)} files passed", flush=True)
    check_documents()
    checked_folders = ["src", "tests", "scripts", "labs"]
    subprocess.run([sys.executable, "-m", "ruff", "check", *checked_folders], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", *checked_folders], cwd=ROOT, check=True
    )
    for folder in ("configs", "examples", "schemas", "labs"):
        for path in (ROOT / folder).rglob("*.json"):
            json.loads(path.read_text())
        for path in (ROOT / folder).rglob("*.toml"):
            tomllib.loads(path.read_text())
    tomllib.loads((ROOT / "pyproject.toml").read_text())
    print("JSON and TOML templates: passed", flush=True)
    subprocess.run(
        [sys.executable, "-m", "atlas_sql_mcp.contracts", "schemas", "--check"],
        cwd=ROOT,
        check=True,
    )
    if options.checks_only:
        return
    if options.integration:
        if any(
            os.environ.get(key) != value
            for key, value in (
                ("ALLOW_SYNTHETIC_DB_WRITES", "1"),
                ("ACCEPT_SQLSERVER_EULA", "Y"),
            )
        ) or not os.environ.get("ATLAS_SQL_DOCKER_IMAGE"):
            parser.error(
                "Integration requires ALLOW_SYNTHETIC_DB_WRITES=1, ACCEPT_SQLSERVER_EULA=Y and ATLAS_SQL_DOCKER_IMAGE"
            )
    with tempfile.TemporaryDirectory(prefix="atlas-verification-") as folder:
        report = Path(folder) / "results.xml"
        subprocess.run(
            [sys.executable, "-m", "pytest", f"--junitxml={report}"], cwd=ROOT, check=True
        )
        if options.integration and any(
            int(s.attrib.get("skipped", 0)) for s in ET.parse(report).iter("testsuite")
        ):
            raise SystemExit(
                "Integration verification failed: skipped tests are not release evidence."
            )


if __name__ == "__main__":
    main()
