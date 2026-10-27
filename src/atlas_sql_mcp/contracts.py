"""Closed JSON Schemas used for MCP discovery and runtime input/output validation."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from .domain import SqlError


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def string(size: int = 256, **kwargs) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": size, **kwargs}


def array(items: dict, maximum: int = 1000) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum}


VERSION = {"const": "1.0"}
ID = string(128)
STRINGS = array(string(4096), 100)
PARAMETER = obj(
    {
        "position": {"type": "integer", "minimum": 1, "maximum": 1000},
        "name": ID,
        "postgresType": string(),
        "nullable": {"type": "boolean"},
        "binding": obj({"filterId": ID, "field": ID}),
    }
)
FIELD = obj(
    {
        "type": {
            "enum": ["text", "boolean", "integer", "decimal", "date", "timestamp", "enum", "array"]
        },
        "parameterPosition": {"type": "integer", "minimum": 1, "maximum": 1000},
        "inclusive": {"type": "boolean"},
        "nullable": {"type": "boolean"},
        "minimum": string(),
        "maximum": string(),
        "scale": {"type": "integer", "minimum": 0, "maximum": 1000},
        "maxLength": {"type": "integer", "minimum": 1, "maximum": 16384},
        "elementType": string(),
        "choices": array(string(), 100),
        "default": {
            "type": ["string", "boolean", "null", "array"],
            "items": {"type": ["string", "boolean"]},
            "maxItems": 1000,
            "maxLength": 16384,
        },
    },
    ["type", "parameterPosition", "nullable"],
)
FILTER = obj(
    {
        "id": ID,
        "label": string(256),
        "kind": {
            "enum": [
                "text",
                "boolean",
                "integer",
                "decimal",
                "date",
                "timestamp",
                "enum",
                "array",
                "date_range",
                "number_range",
                "timestamp_range",
            ]
        },
        "required": {"type": "boolean"},
        "fields": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 2,
            "propertyNames": ID,
            "additionalProperties": FIELD,
        },
        "validation": obj({"ordering": {"enum": ["from < until", "from <= until"]}}, []),
    },
    ["id", "label", "kind", "required", "fields"],
)
OUTPUT_DECLARATION = obj(
    {"key": ID, "label": string(256), "postgresType": string(), "unit": string(128)},
    ["key", "label"],
)
SEMANTICS = obj(
    {
        "grain": array(ID, 100),
        "countedEntities": STRINGS,
        "measures": array(
            obj(
                {"outputKey": ID, "definition": string(4096), "unit": string(128)},
                ["outputKey", "definition"],
            ),
            1000,
        ),
        "assumptions": STRINGS,
        "resolvedDecisions": STRINGS,
    },
    ["grain", "countedEntities", "measures", "assumptions", "resolvedDecisions"],
)
CONTEXT_INPUT = obj(
    {
        "contractVersion": VERSION,
        "connectionId": ID,
        "request": string(16384),
        "contextId": string(128),
        "cursor": string(128),
        "focus": obj(
            {
                "search": string(1024),
                "relations": array(ID, 100),
                "include": array({"enum": ["columns", "relationships", "businessDefinitions"]}, 3),
            },
            [],
        ),
        "pageSize": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    ["contractVersion", "connectionId", "request"],
)
FINALIZE_INPUT = obj(
    {
        "contractVersion": VERSION,
        "contextId": ID,
        "sql": string(262144),
        "parameters": array(PARAMETER),
        "filters": array(FILTER),
        "outputs": array(OUTPUT_DECLARATION),
        "semantics": SEMANTICS,
    }
)
ERROR = obj(
    {
        "status": {"const": "error"},
        "error": obj(
            {
                "code": string(),
                "message": string(4096),
                "fieldPath": {"type": "string", "maxLength": 4096},
                "retryable": {"type": "boolean"},
            }
        ),
    }
)
COLUMN = obj(
    {
        "name": string(),
        "postgresType": string(),
        "nullable": {"type": "boolean"},
        "description": {"type": "string", "maxLength": 4096},
        "descriptionProvenance": {"const": "untrusted_database_comment"},
        "typeSchema": string(),
    }
)
CONSTRAINT = obj(
    {
        "name": string(),
        "kind": {"enum": ["p", "u", "f"]},
        "columns": array(string()),
        "targetSchema": {"type": "string"},
        "targetRelation": {"type": "string"},
        "targetColumns": array(string()),
        "validated": {"type": "boolean"},
        "enforced": {"type": "boolean"},
    }
)
RELATION = obj(
    {
        "schema": string(),
        "name": string(),
        "kind": {"const": "table"},
        "columns": array(COLUMN, 10000),
        "relationships": array(CONSTRAINT, 1000),
        "description": {"type": "string", "maxLength": 4096},
        "descriptionProvenance": {"const": "untrusted_database_comment"},
        "inheritedReadsRequireAuthorization": {"type": "boolean"},
    }
)
CAPABILITIES = obj(
    {
        "postgresMajorVersions": array({"type": "integer"}, 10),
        "features": array(string(), 100),
        "unsupported": array(string(), 100),
        "requiredSessionSettings": obj({"search_path": {"const": "pg_catalog"}}),
        "limits": obj(
            {
                k: {"type": "integer", "minimum": 1}
                for k in [
                    "sqlBytes",
                    "inputBytes",
                    "outputBytes",
                    "astNodes",
                    "astDepth",
                    "contextSeconds",
                    "analysisSeconds",
                ]
            }
        ),
        "businessRowsRead": {"const": False},
        "queryExecutionAvailable": {"const": False},
        "parameterCastsRequired": {"const": True},
        "approvedFunctions": array(string(), 200),
    }
)
CONTEXT_SUCCESS = obj(
    {
        "status": {"const": "context"},
        "contractVersion": VERSION,
        "contextId": ID,
        "expiresAt": string(),
        "connectionId": ID,
        "dialect": {"const": "postgresql"},
        "postgresMajorVersion": {"const": 18},
        "timezone": string(),
        "clock": string(),
        "metadataFingerprint": string(64),
        "policyVersion": string(64),
        "capabilities": CAPABILITIES,
        "relations": array(RELATION, 100),
        "businessDefinitions": array(obj({"status": {"const": "not_configured"}}), 1),
        "coverage": {"enum": ["partial", "complete"]},
        "omittedDetails": array(string(), 10),
        "nextCursor": {"type": ["string", "null"], "maxLength": 128},
    }
)
DEPENDENCIES = obj(
    {
        "relations": array(string()),
        "columns": array(string(), 10000),
        "functions": array(string()),
        "metadataFingerprint": string(64),
        "policyVersion": string(64),
    }
)
VALIDATION = obj(
    {
        "syntax": {"const": "passed"},
        "resolution": {"const": "passed_supported_subset"},
        "authorization": {"const": "passed"},
        "readOnlyPolicy": {"const": "passed"},
        "bindings": {"const": "passed"},
        "semantics": {"const": "declared_rules_only"},
        "executionPerformed": {"const": False},
        "costAssessment": {"const": "not_performed"},
        "businessEquivalence": {"const": "not_proven"},
        "verifiedAt": string(),
        "contextExpiresAt": string(),
        "limitations": STRINGS,
    }
)
DERIVED_OUTPUT = obj(
    {
        "key": ID,
        "label": string(256),
        "postgresType": string(),
        "logicalType": string(),
        "nullable": {"type": ["boolean", "null"]},
        "lineage": array(string(), 10000),
        "unit": string(128),
    },
    ["key", "label", "postgresType", "logicalType", "nullable", "lineage"],
)
ARTIFACT = obj(
    {
        "contractVersion": VERSION,
        "dialect": {"const": "postgresql"},
        "postgresMajorVersion": {"const": 18},
        "sql": string(262144),
        "artifactHash": string(64),
        "parameters": array(PARAMETER),
        "filters": array(FILTER),
        "outputs": array(DERIVED_OUTPUT),
        "semantics": SEMANTICS,
        "dependencies": DEPENDENCIES,
        "requiredSessionSettings": obj({"search_path": {"const": "pg_catalog"}}),
        "validation": VALIDATION,
        "warnings": array(obj({"code": string(), "message": string(4096)}), 1000),
        "explanation": string(4096),
    }
)
FINALIZE_SUCCESS = obj({"status": {"const": "validated"}, "artifact": ARTIFACT})
# Contract 1.0 is unchanged for PostgreSQL. SQL Server uses 1.1 and neutral
# sqlType/databaseMajorVersion names, never PostgreSQL-labelled T-SQL metadata.


def sqlserver_schema(schema):
    if isinstance(schema, list):
        return [sqlserver_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return "sqlType" if schema == "postgresType" else schema
    result = {}
    for key, value in schema.items():
        key = {
            "postgresType": "sqlType",
            "postgresMajorVersion": "databaseMajorVersion",
            "postgresMajorVersions": "databaseMajorVersions",
        }.get(key, key)
        if key == "contractVersion":
            result[key] = {"const": "1.1"}
        elif key == "dialect":
            result[key] = {"const": "sqlserver"}
        elif key == "databaseMajorVersion":
            result[key] = {"enum": [16, 17]}
        elif key == "requiredSessionSettings":
            result[key] = obj(
                {
                    k: {"const": "ON"}
                    for k in ("QUOTED_IDENTIFIER", "ANSI_NULLS", "ANSI_WARNINGS", "ARITHABORT")
                }
            )
        else:
            result[key] = sqlserver_schema(value)
    # Required property names are data, not dict keys.
    if isinstance(result.get("required"), list):
        result["required"] = [
            {
                "postgresMajorVersion": "databaseMajorVersion",
                "postgresMajorVersions": "databaseMajorVersions",
            }.get(k, k)
            for k in result["required"]
        ]
    return result


SQLSERVER_CONTEXT = sqlserver_schema(CONTEXT_SUCCESS)
SQLSERVER_ARTIFACT = sqlserver_schema(ARTIFACT)
SQLSERVER_ARTIFACT["properties"]["validation"]["properties"]["syntax"] = {
    "const": "passed_static_subset"
}
SQLSERVER_FINALIZE_INPUT = sqlserver_schema(FINALIZE_INPUT)
SQLSERVER_FINALIZE_SUCCESS = obj({"status": {"const": "validated"}, "artifact": SQLSERVER_ARTIFACT})
MULTI_CONTEXT_INPUT = deepcopy(CONTEXT_INPUT)
MULTI_CONTEXT_INPUT["properties"]["contractVersion"] = {"enum": ["1.0", "1.1"]}
MULTI_FINALIZE_INPUT = deepcopy(FINALIZE_INPUT)
MULTI_FINALIZE_INPUT["properties"]["contractVersion"] = {"enum": ["1.0", "1.1"]}
MULTI_FINALIZE_INPUT["properties"]["parameters"] = array(
    {"oneOf": [PARAMETER, sqlserver_schema(PARAMETER)]}
)
MULTI_FINALIZE_INPUT["properties"]["outputs"] = array(
    {"anyOf": [OUTPUT_DECLARATION, sqlserver_schema(OUTPUT_DECLARATION)]}
)
MULTI_FINALIZE_INPUT["allOf"] = [
    {
        "if": {"properties": {"contractVersion": {"const": version}}},
        "then": {
            "properties": {
                "parameters": variant["properties"]["parameters"],
                "outputs": variant["properties"]["outputs"],
            }
        },
    }
    for version, variant in [("1.0", FINALIZE_INPUT), ("1.1", SQLSERVER_FINALIZE_INPUT)]
]


# Version 1.2 uses the same neutral field names for every supported dialect.
# Keep legacy schemas intact so existing consumers can migrate explicitly.
def neutral_schema(schema):
    if isinstance(schema, list):
        return [neutral_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return (
            {
                "postgresType": "sqlType",
                "postgresMajorVersion": "databaseMajorVersion",
                "postgresMajorVersions": "databaseMajorVersions",
            }.get(schema, schema)
            if isinstance(schema, str)
            else schema
        )
    result = {}
    for key, value in schema.items():
        neutral_key = {
            "postgresType": "sqlType",
            "postgresMajorVersion": "databaseMajorVersion",
            "postgresMajorVersions": "databaseMajorVersions",
        }.get(key, key)
        result[neutral_key] = (
            {"const": "1.2"} if key == "contractVersion" else neutral_schema(value)
        )
    return result


NEUTRAL_FINALIZE_INPUT = neutral_schema(FINALIZE_INPUT)
NEUTRAL_CONTEXTS = [neutral_schema(CONTEXT_SUCCESS), neutral_schema(SQLSERVER_CONTEXT)]
NEUTRAL_SUCCESSES = [neutral_schema(FINALIZE_SUCCESS), neutral_schema(SQLSERVER_FINALIZE_SUCCESS)]
MULTI_CONTEXT_INPUT["properties"]["contractVersion"]["enum"].append("1.2")
MULTI_FINALIZE_INPUT["properties"]["contractVersion"]["enum"].append("1.2")
MULTI_FINALIZE_INPUT["allOf"].append(
    {
        "if": {"properties": {"contractVersion": {"const": "1.2"}}},
        "then": {
            "properties": {
                "parameters": NEUTRAL_FINALIZE_INPUT["properties"]["parameters"],
                "outputs": NEUTRAL_FINALIZE_INPUT["properties"]["outputs"],
            }
        },
    }
)


CONNECT_INPUT = obj(
    {"connectionString": {**string(16384), "writeOnly": True}, "schemas": array(ID, 100)},
    ["connectionString"],
)
CONNECT_SUCCESS = obj(
    {
        "status": {"const": "connected"},
        "connectionId": ID,
        "dialect": {"enum": ["postgresql", "sqlserver"]},
        "contractVersion": {"enum": ["1.0", "1.1"]},
        "expiresAt": string(),
        "relationCount": {"type": "integer", "minimum": 1, "maximum": 1000},
    }
)
SCHEMAS = {
    "atlas_sql_connect": (CONNECT_INPUT, {"type": "object", "oneOf": [CONNECT_SUCCESS, ERROR]}),
    "atlas_sql_context_get": (
        MULTI_CONTEXT_INPUT,
        {"type": "object", "oneOf": [CONTEXT_SUCCESS, SQLSERVER_CONTEXT, *NEUTRAL_CONTEXTS, ERROR]},
    ),
    "atlas_sql_finalize": (
        MULTI_FINALIZE_INPUT,
        {
            "type": "object",
            "oneOf": [FINALIZE_SUCCESS, SQLSERVER_FINALIZE_SUCCESS, *NEUTRAL_SUCCESSES, ERROR],
        },
    ),
}


def validate(schema: dict, value: object) -> None:
    errors = Draft202012Validator(schema).iter_errors(value)
    error = next(errors, None)
    if error:
        path = ".".join(map(str, error.absolute_path))
        # jsonschema messages may echo sensitive input: do not return them.
        message = "Input does not match the published contract."
        if error.validator == "additionalProperties":
            message += (
                " Remove undeclared fields; use only properties in the published input schema."
            )
        elif error.validator == "required":
            missing = sorted(set(error.validator_value) - error.instance.keys())
            message += " Required fields: " + ", ".join(missing) + "."
        raise SqlError("INVALID_CONTRACT", message, path)


def export_schemas(directory, check=False):
    """Export the same schemas the server enforces; --check detects stale copies."""
    import json
    from pathlib import Path

    directory = Path(directory)
    if not check:
        directory.mkdir(parents=True, exist_ok=True)
    from .generation import GENERATE_SCHEMAS

    for name, pair in (SCHEMAS | GENERATE_SCHEMAS).items():
        for suffix, schema in zip(("input", "output"), pair, strict=True):
            path = directory / f"{name}.{suffix}.json"
            encoded = (
                json.dumps(
                    {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            if check:
                if not path.exists() or path.read_text() != encoded:
                    raise SystemExit(f"Stale or missing schema: {path.name}")
            else:
                path.write_text(encoded)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    export_schemas(arguments.directory, arguments.check)
