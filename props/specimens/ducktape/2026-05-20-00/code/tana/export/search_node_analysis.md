# Search Node Structure in Tana JSON Exports: Detailed Analysis

## Overview

This document analyzes how Tana exports search nodes and their query semantics in JSON format. The analysis is based on examining real search nodes from Tana exports to understand what information is preserved and how queries could be reconstructed.

## 1. Core Search Node Properties

Every search node in a Tana export has these fundamental properties:

```json
{
  "id": "unique_node_id",
  "name": "Search name visible to user",
  "children": ["result_id_1", "result_id_2", ...],
  "_docType": "search",
  "_metaNodeId": "metadata_node_id",
  "_ownerId": "parent_container_id",
  "createdAt": 1234567890,
  "editedAt": 1234567890,
  "touchCounts": {...},
  "modifiedTs": {...}
}
```

Key observations:

- `_docType: "search"` identifies search nodes
- Search results are stored as direct children
- The actual search expression is stored separately in the metadata node

## 2. Search Expression Storage Architecture

The search query is not stored directly in the search node. Instead, it follows this indirection pattern:

```
Search Node
  └── _metaNodeId → Metadata Node
                      └── children[0] → Tuple Node
                                          ├── children[0] = "SYS_A15" (Search expression key)
                                          └── children[1+] = Expression components
```

This architecture separates the search results (stored as children) from the search definition (stored in metadata).

## 3. Query Language Components

Tana uses special system nodes for search operators and types:

### Boolean Operators

- **`SYS_A15`** - Search expression attribute key (always first in the tuple)
- **`SYS_A41`** - AND operator
- **`SYS_A42`** - OR operator
- **`SYS_A43`** - NOT operator

### System Types

- **`SYS_T103`** - Event type
- **`SYS_T98`** - Meeting type
- Additional system types follow the pattern `SYS_T{number}`

## 4. Search Expression Patterns

### Pattern 1: Simple Tag Search

The simplest search type - finding all nodes with a specific tag:

```
Metadata Node
  └── Tuple Node
      ├── "SYS_A15" (Search expression key)
      └── "tag_node_id" (The tag to search for)
```

Example: Searching for `#issue` tag

### Pattern 2: Boolean Expression

For searches with boolean logic:

```
Metadata Node
  └── Tuple Node
      ├── "SYS_A15"
      └── OR Node
          └── Tuple Node
              ├── "SYS_A42" (OR operator)
              ├── "criteria1_node_id"
              ├── "criteria2_node_id"
              └── ...
```

Example: `OR(event, meeting, "FROM CALENDAR")`

### Pattern 3: Complex Nested Expression

Boolean expressions can be nested to arbitrary depth:

```
AND(
  tag1,
  OR(
    tag2,
    NOT(tag3)
  )
)
```

## 5. Search Results Representation

### Result Storage

- Results are stored as direct children of the search node
- Each child ID references a node elsewhere in the node store
- Results maintain their original ownership (not owned by the search node)
- Large result sets (1000+ results) are fully enumerated

### Result Ordering

- The order of children in the array represents the result order
- This preserves any sorting applied by Tana

## 6. Additional Search Metadata

### Association Maps

Used for table views and contextual data:

```json
"associationMap": {
  "result_node_id": "associated_data_node_id",
  "another_result_id": "another_data_node_id"
}
```

This allows searches to attach additional data to specific results (e.g., column values in table views).

### Search Context

Defines the scope for the search:

```json
"searchContextNode": "context_node_id"
```

Example use case: Searching within a specific journal date or project.

### View Configuration

Some searches include view definitions in their metadata:

- Layout type (table, list, etc.)
- Column definitions for table views
- Grouping and sorting preferences

## 7. Special Search Types

### Calendar Searches

Searches for calendar items use text criteria:

- Node with name "FROM CALENDAR" as a search criterion
- Combined with type filters (event, meeting) using OR

### Saved Searches

- Stored in special containers (e.g., `{workspace_id}_SEARCHES`)
- Include usage tracking via `touchCounts`
- Can be referenced and reused

## 8. Query Semantics Preservation

The export format preserves:

1. **Complete boolean logic** - All AND/OR/NOT operations with proper nesting
2. **Tag and type references** - Via node IDs that can be resolved
3. **Text-based criteria** - Stored as nodes with the search text as name
4. **Search scope** - Via searchContextNode
5. **Result ordering** - Via children array order
6. **Result metadata** - Via associationMap
7. **View preferences** - In metadata node

## 9. Examples from Real Data

### Example 1: Simple Tag Search

Search: "Search results for #issue"

```
Search Node (SGrUB4iyWYde)
  └── Metadata (UPQzRt-7yPuY)
      └── Tuple (iA36z7SvNO)
          ├── "SYS_A15"
          └── "xTcTNuPqb8" (issue tag)
```

### Example 2: Complex OR Search

Search: "Agenda"

```
Search Node (lRiDEA6lQM)
  └── Metadata (uDnQ7Y7qR1d0)
      └── Tuple (-OjqkA37F5)
          ├── "SYS_A15"
          └── OR Node (Sn0-KlTVRU)
              └── Tuple (aJYKJ19yTY)
                  ├── "SYS_A42" (OR)
                  ├── "SYS_T103" (event)
                  ├── "SYS_T98" (meeting)
                  └── "qvgxOipNOT" ("FROM CALENDAR")
```

## 10. Reconstruction Capabilities

With this structure, you can fully reconstruct:

1. **The original search query** - By parsing the expression tree
2. **Boolean logic** - Including nested operations
3. **All search criteria** - Tags, types, and text searches
4. **Search context** - Where the search was scoped
5. **Result presentation** - How results should be displayed
6. **Result metadata** - Additional data attached to results

### Limitations

The main limitation is that these are **static snapshots** - the search results are frozen at export time and won't update dynamically. The search expression is preserved, but would need to be re-executed against current data to get fresh results.

## Conclusion

Tana's JSON export format preserves complete search semantics through a well-structured system of:

- Search nodes with result children
- Metadata nodes containing search expressions
- System nodes representing operators and types
- Association maps for result metadata

This architecture allows for full reconstruction of search queries while maintaining a clean separation between search definitions and their results.
