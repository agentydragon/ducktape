# Tana Search Materialization

Compare a saved search's stored results with the results of evaluating its expression
against the same Tana JSON export. These commands print to stdout and leave the export file intact.

```bash
bb run //tana/export:materialize_searches_bin -- "$PWD/export.json" --list
bb run //tana/export:materialize_searches_bin -- "$PWD/export.json" --search-id SEARCH_ID
bb run //tana/export:materialize_searches_bin -- "$PWD/export.json" --all
```

Use `--list` to find the ID. The report prints the parsed expression, both result counts,
and up to ten missing and ten extra nodes. The comparison uses node IDs and ignores result
ordering.

The evaluator supports tags, field values, text, boolean expressions, and event and meeting types.
It follows view-embedded search definitions, scopes results to the search context when available,
and resolves a field value of `PARENT` to the containing node's name when present. Some Tana
operators, including `DATE OVERLAPS` and `FROM CALENDAR`, return no matches.

See the [search-node format notes](search_node_analysis.md) for stored fields and system IDs.
