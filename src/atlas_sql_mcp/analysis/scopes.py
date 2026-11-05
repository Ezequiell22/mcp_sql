"""Dialect-neutral scope, grouping and join-grain proofs."""

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from atlas_sql_mcp.domain import Catalog, SqlError


def _source_keys(source: exp.Table | Scope, catalog: Catalog) -> list[frozenset[str]]:
    if isinstance(source, exp.Table):
        relation = catalog.relation_map.get(f"{source.db}.{source.name}")
        return (
            [
                frozenset(c.columns)
                for c in relation.constraints
                if c.kind in {"p", "u"}
                and c.validated
                and c.enforced
                and all(not relation.column_map[n].nullable for n in c.columns)
            ]
            if relation
            else []
        )
    query = source.expression
    if not isinstance(query, exp.Select):
        return []
    groups = query.args.get("group")
    if (
        groups
        and groups.expressions
        and all(isinstance(e, exp.Column) for e in groups.expressions)
        and not any(groups.args.get(k) for k in ("rollup", "cube", "grouping_sets"))
    ):
        selected = {
            p.this.sql(): p.alias_or_name for p in query.selects if isinstance(p, exp.Alias)
        }
        keys = [selected.get(g.sql()) for g in groups.expressions]
        if all(keys):
            return [frozenset(keys)]
    if query.args.get("distinct") and not query.args["distinct"].args.get("on"):
        return [frozenset(p.alias_or_name for p in query.selects)]
    # Preserve direct projections of a base unique key only without joins.
    if not query.args.get("joins") and len(source.selected_sources) == 1:
        alias, (_, underlying) = next(iter(source.selected_sources.items()))
        if underlying is source:
            return []
        mapped = {
            p.this.name: p.alias_or_name
            for p in query.selects
            if isinstance(p, exp.Alias) and isinstance(p.this, exp.Column) and p.this.table == alias
        }
        return [
            frozenset(mapped[c] for c in key)
            for key in _source_keys(underlying, catalog)
            if key <= mapped.keys()
        ]
    return []


def _own(node: exp.Expr, scope: Scope) -> bool:
    return node.find_ancestor(exp.Select, exp.SetOperation) is scope.expression


def check_scopes(scopes: list[Scope], catalog: Catalog) -> list[dict]:
    warnings = []
    for scope in scopes:
        query = scope.expression
        if not isinstance(query, exp.Select):
            continue
        aliases = {a: s for a, (_, s) in scope.selected_sources.items()}
        joins = query.args.get("joins", [])
        pairs: dict[tuple[str, str], set[str]] = {}
        for join in joins:
            if join.args.get("method") == "NATURAL" or join.args.get("using"):
                raise SqlError(
                    "UNSUPPORTED_SQL_FEATURE",
                    "Use explicit JOIN ON predicates instead of NATURAL/USING.",
                    "sql",
                )
            predicate = join.args.get("on")
            if not predicate:
                raise SqlError(
                    "UNSUPPORTED_SQL_FEATURE",
                    "Cartesian joins are not enabled by this policy.",
                    "sql",
                )
            # OR conditions do not prove a unique-key equality.
            if not any(predicate.find_all(exp.Or)):
                for eq in predicate.find_all(exp.EQ):
                    if eq.find_ancestor(exp.Select) is not query:
                        continue
                    path = eq
                    while path is not predicate and isinstance(path.parent, (exp.And, exp.Paren)):
                        path = path.parent
                    if path is not predicate:
                        continue
                    if (
                        isinstance(eq.this, exp.Column)
                        and isinstance(eq.expression, exp.Column)
                        and eq.this.table != eq.expression.table
                    ):
                        a, b = eq.this, eq.expression
                        if a.table in aliases and b.table in aliases:
                            pairs.setdefault((a.table, b.table), set()).add(b.name)
                            pairs.setdefault((b.table, a.table), set()).add(a.name)
        edges = {
            a: {
                b
                for b, source in aliases.items()
                if b != a
                and any(k <= pairs.get((a, b), set()) for k in _source_keys(source, catalog))
            }
            for a in aliases
        }

        def preserves_grain(start: str, edges=edges, expected_aliases=frozenset(aliases)) -> bool:
            reached, pending = set(), [start]
            while pending:
                alias = pending.pop()
                if alias not in reached:
                    reached.add(alias)
                    pending.extend(edges.get(alias, ()))
            return reached == expected_aliases

        unique = {a: preserves_grain(a) for a in aliases}
        # Only unique-key paths reachable from each measured source prove that
        # additional relations cannot multiply its rows. A key equality between
        # two other aliases must never "prove" the original one-to-many join safe.
        all_aggregates = [a for a in query.find_all(exp.AggFunc) if _own(a, scope)]
        aggregates = [a for a in all_aggregates if not a.find_ancestor(exp.Window)]
        for aggregate in all_aggregates:
            if isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct):
                continue
            measured = {c.table for c in aggregate.find_all(exp.Column)}
            if len(aliases) > 1 and (
                not measured or any(not unique.get(a, False) for a in measured)
            ):
                raise SqlError(
                    "JOIN_FANOUT_RISK",
                    "Aggregate grain can be multiplied by joins. Preaggregate, use EXISTS, or provide a provably unique join.",
                    "sql",
                )
        if joins and not all(unique.values()):
            warnings.append(
                {
                    "code": "JOIN_CARDINALITY",
                    "message": "Join output may contain repeated entity keys; inspect the declared result grain.",
                }
            )
        nonaggregate_clauses = [query.args.get(clause) for clause in ("where", "group")]
        nonaggregate_clauses.extend(join.args.get("on") for join in joins)
        for part in nonaggregate_clauses:
            if part and any(_own(a, scope) for a in part.find_all(exp.AggFunc)):
                raise SqlError("SQL_TYPE_ERROR", "Aggregate is not valid in this clause.", "sql")
        group = query.args.get("group")
        if aggregates or group or query.args.get("having"):
            grouping = list(group.expressions) if group else []
            expanded = []
            for g in grouping:
                if isinstance(g, (exp.Rollup, exp.Cube, exp.GroupingSets, exp.Tuple)):
                    expanded.extend(g.find_all(exp.Column))
                else:
                    expanded.append(g)
            grouping = expanded
            if group:
                for extra in ("rollup", "cube", "grouping_sets"):
                    for item in group.args.get(extra, []):
                        grouping.extend(item.find_all(exp.Column))
            simple_group_columns = {
                (c.table, c.name) for c in grouping if isinstance(c, exp.Column)
            }
            group_sql = {g.sql() for g in grouping}
            checked_expressions = list(query.selects)
            if query.args.get("having"):
                checked_expressions.append(query.args["having"].this)
            if query.args.get("order"):
                for ordered in query.args["order"].expressions:
                    expression = ordered.this
                    # Qualification can rewrite ORDER BY base columns into output
                    # aliases. Validate the original projection at this query level,
                    # rather than mistaking its alias for an ungrouped base column.
                    if isinstance(expression, exp.Column) and not expression.table:
                        matches = [p for p in query.selects if p.alias_or_name == expression.name]
                        if len(matches) == 1:
                            expression = matches[0]
                    checked_expressions.append(expression)
            for projected in checked_expressions:
                expression = projected.this if isinstance(projected, exp.Alias) else projected
                if expression.sql() in group_sql:
                    continue
                for col in expression.find_all(exp.Column):
                    filtered = col.find_ancestor(exp.Filter)
                    if (
                        col.find_ancestor(exp.AggFunc)
                        or (filtered and isinstance(filtered.this, exp.AggFunc))
                        or col.find_ancestor(exp.Select) is not query
                    ):
                        continue
                    key = (col.table, col.name)
                    # Grouped expressions do not imply grouping by their components.
                    allowed = key in simple_group_columns
                    if not allowed and col.table in aliases:
                        grouped = {n for a, n in simple_group_columns if a == col.table}
                        source = aliases[col.table]
                        if isinstance(source, exp.Table):
                            relation = catalog.relation_map[f"{source.db}.{source.name}"]
                            allowed = any(
                                c.kind == "p"
                                and c.validated
                                and c.enforced
                                and set(c.columns) <= grouped
                                for c in relation.constraints
                            )
                    if not allowed:
                        raise SqlError(
                            "AMBIGUOUS_GRAIN",
                            "Projected expression is not grouped or functionally dependent on a grouped key.",
                            "sql",
                        )
    return warnings
