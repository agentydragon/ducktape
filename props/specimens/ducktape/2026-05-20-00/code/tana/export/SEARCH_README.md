# Tana Search Materialization

This tool re-executes search nodes from Tana JSON exports to analyze and compare their results.

## Overview

Tana exports contain search nodes with their results frozen at export time. This tool parses the search expressions and re-executes them against the node store to:

- Understand search semantics
- Compare stored vs current results
- Debug search expressions
- Analyze search criteria

## Usage

```bash
# List all search nodes in an export
python materialize_searches.py export.json --list

# Materialize a specific search
python materialize_searches.py export.json --search-id SEARCH_ID

# Process all searches
python materialize_searches.py export.json --all
```

## Features

### Search Expression Types

- **Tag searches**: Find nodes with specific tags (e.g., `#issue`)
- **Field searches**: Match field values (e.g., `Status = "Open"`)
- **Boolean logic**: AND, OR, NOT operators
- **Type searches**: System types like events and meetings
- **Nested expressions**: Complex boolean combinations

### Special Features

- **PARENT resolution**: Searches can use `PARENT` as a placeholder for the containing node
- **Context searches**: Searches can be scoped to descendants of a specific node
- **View-embedded searches**: Handles searches defined within view definitions

## Example

```bash
$ python materialize_searches.py tana_export.json --search-id QJYSvmW5NdlY

Search: Do next & Open
ID: QJYSvmW5NdlY
Expression: AND(
  TagSearch(#issue),
  FieldSearch(Status = "Open"),
  FieldSearch(Hotlists = "Do next")
)

Stored results: 28
Materialized results: 28
✅ Stored and materialized results match!
```

## Architecture

The implementation consists of:

1. **Search Parser** (`tana/query/search_parser.py`)
   - Extracts search expressions from metadata
   - Parses boolean logic and search criteria
   - Handles nested view definitions

2. **Search Evaluator** (`tana/query/search_evaluator.py`)
   - Executes parsed expressions
   - Resolves PARENT references
   - Applies boolean operators
   - Filters by tags, fields, and types

3. **Search Materializer** (`tana/query/search_materializer.py`)
   - Combines parser and evaluator
   - Compares stored vs re-executed results
   - Provides analysis of differences

## Limitations

- Some Tana operators not yet supported (DATE OVERLAPS, FROM CALENDAR, etc.)
- Search results may differ if nodes have been modified since export
- Context-dependent searches require proper parent node resolution
