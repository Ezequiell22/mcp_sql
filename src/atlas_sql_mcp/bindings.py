"""Lossless filter contract validation, separate from SQL parsing and transport."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .analyzer import logical_type, normalize_type
from .domain import SqlError


def _fail(message: str, field: str = "filters") -> None:
    raise SqlError("PARAMETER_MISMATCH", message, field)


def _default(field: dict, datatype: str) -> None:
    if "default" not in field:
        return
    value = field["default"]
    if value is None:
        if not field["nullable"]:
            _fail("Null default requires a nullable field.")
        return
    if datatype.endswith("[]"):
        if not isinstance(value, list) or len(value) > 1000:
            _fail("Array defaults must be bounded arrays.")
        nested = {"type": logical_type(datatype[:-2]), "nullable": False}
        for item in value:
            _default({**nested, "default": item}, datatype[:-2])
        return
    if datatype == "boolean":
        if type(value) is not bool:
            _fail("Boolean defaults require a JSON boolean.")
        return
    if not isinstance(value, str) or len(value) > field.get("maxLength", 16384):
        _fail("Defaults require bounded lossless strings.")
    try:
        kind = logical_type(datatype)
        if kind in {"integer", "decimal"}:
            number = Decimal(value)
            if not number.is_finite() or (
                kind == "integer" and (not re.fullmatch(r"-?\d+", value))
            ):
                _fail("Invalid exact numeric default.")
            bounds = {
                "smallint": (-32768, 32767),
                "integer": (-2147483648, 2147483647),
                "bigint": (-9223372036854775808, 9223372036854775807),
            }
            if datatype in bounds and not bounds[datatype][0] <= number <= bounds[datatype][1]:
                _fail("Integer default exceeds its PostgreSQL type.")
            if "minimum" in field and number < Decimal(field["minimum"]):
                _fail("Default is below minimum.")
            if "maximum" in field and number > Decimal(field["maximum"]):
                _fail("Default exceeds maximum.")
            if "scale" in field and -number.as_tuple().exponent > field["scale"]:
                _fail("Default exceeds declared decimal scale.")
        elif kind == "date":
            date.fromisoformat(value)
            if len(value) != 10:
                _fail("Dates require YYYY-MM-DD.")
        elif kind == "timestamp":
            parsed = datetime.fromisoformat(value)
            aware = parsed.tzinfo is not None and parsed.utcoffset() is not None
            if aware != (datatype == "timestamp with time zone"):
                _fail("Timestamp timezone does not match its PostgreSQL type.")
        elif datatype == "uuid":
            from uuid import UUID

            UUID(value)
        if field["type"] == "enum" and value not in field.get("choices", []):
            _fail("Default is not an allowed enumeration value.")
    except (ValueError, InvalidOperation):
        _fail("Default value does not match its declared type.")


def validate_bindings(
    parameters: list[dict],
    filters: list[dict],
    analyzed: dict[int, str],
    *,
    type_key="postgresType",
    normalize=normalize_type,
    logical=logical_type,
    default_check=_default,
) -> None:
    by_position = {p["position"]: p for p in parameters}
    if len(by_position) != len(parameters) or set(by_position) != set(analyzed):
        _fail("Declared parameter positions must exactly match the SQL.", "parameters")
    if len({p["name"] for p in parameters}) != len(parameters):
        _fail("Parameter names must be unique.", "parameters")
    by_id = {f["id"]: f for f in filters}
    if len(by_id) != len(filters):
        _fail("Filter IDs must be unique.")
    fields = {}
    for item in filters:
        ranged = item["kind"].endswith("_range")
        if (ranged and set(item["fields"]) != {"from", "until"}) or (
            not ranged and set(item["fields"]) != {"value"}
        ):
            _fail("Ranges require from/until fields; scalar filters require value.")
        if ranged and "validation" not in item:
            _fail("Ranges require an explicit ordering rule.")
        for name, field in item["fields"].items():
            position = field["parameterPosition"]
            parameter = by_position.get(position)
            if not parameter or position in fields:
                _fail("Every filter field must bind to one unique parameter position.")
            fields[position] = field
            if parameter["binding"] != {"filterId": item["id"], "field": name}:
                _fail("Filter and parameter bindings disagree.")
            datatype = normalize(parameter[type_key])
            if datatype != analyzed[position] or parameter["nullable"] != field["nullable"]:
                _fail("Parameter type or nullability disagrees with its SQL cast/field.")
            kind = logical(datatype)
            expected_kind = {"identifier": "text", "json": "text"}.get(kind, kind)
            if field["type"] != expected_kind and not (
                field["type"] == "enum" and expected_kind == "text"
            ):
                _fail("Filter field kind is incompatible with its PostgreSQL type.")
            if ranged and (field.get("nullable") or not item["required"]):
                _fail("This version requires nonnullable, required range endpoints.")
            if not item["required"] and not field["nullable"] and "default" not in field:
                _fail("Optional fields require a default or explicit SQL null behavior.")
            if field["nullable"]:
                # Null-safe predicate verification requires more than a declaration;
                # avoid accepting optional filters whose null value drops all rows.
                _fail(
                    "Nullable runtime parameters are not enabled; use a required value or nonnull default."
                )
            if ranged and item["kind"] in {"date_range", "timestamp_range"}:
                if field.get("inclusive") != (name == "from"):
                    _fail("Temporal ranges must include from and exclude until.")
            if ranged:
                expected = {
                    "date_range": "date",
                    "timestamp_range": "timestamp",
                    "number_range": None,
                }[item["kind"]]
                if (expected and field["type"] != expected) or (
                    not expected and field["type"] not in {"integer", "decimal"}
                ):
                    _fail("Range kind disagrees with its endpoints.")
            elif item["kind"] != field["type"]:
                _fail("Scalar filter kind disagrees with its field.")
            if "choices" in field and field["type"] != "enum":
                _fail("Choices are only valid for enum fields.")
            if field["type"] == "enum" and not field.get("choices"):
                _fail("Enum filters require static approved choices.")
            if kind == "array" and normalize_type(field.get("elementType", "")) != datatype[:-2]:
                _fail("Array filters require the exact PostgreSQL element type.")
            for bound in ("minimum", "maximum"):
                if bound in field:
                    try:
                        if not Decimal(field[bound]).is_finite():
                            _fail("Numeric bounds must be finite decimal strings.")
                    except InvalidOperation:
                        _fail("Numeric bounds must be finite decimal strings.")
            if (
                "minimum" in field
                and "maximum" in field
                and Decimal(field["minimum"]) > Decimal(field["maximum"])
            ):
                _fail("Minimum exceeds maximum.")
            default_check(field, datatype)
        if ranged:
            a, b = (item["fields"][n] for n in ("from", "until"))
            if a["type"] != b["type"]:
                _fail("Range endpoint types must agree.")
            if "default" in a and "default" in b:
                av, bv = a["default"], b["default"]
                if a["type"] in {"integer", "decimal"}:
                    av, bv = Decimal(av), Decimal(bv)
                elif a["type"] == "timestamp":
                    av, bv = datetime.fromisoformat(av), datetime.fromisoformat(bv)
                ordering = item["validation"]["ordering"]
                if av > bv or (av == bv and ordering == "from < until"):
                    _fail("Range defaults violate the ordering rule.")
    if set(fields) != set(by_position):
        _fail("All parameters must bind to filter fields.")
