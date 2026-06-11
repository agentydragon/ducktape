# Scan: Denormalized and Computed Fields

## Problem

Data structures should not contain both canonical data and derived/computed data. If a field can be computed from other fields (i.e., `bar = f(foo)`), it should not be stored alongside the original data.

## Why This Matters

1. **Redundancy**: Storing both `foo` and `f(foo)` duplicates information
2. **Maintenance burden**: Changes to computation logic require updating all existing data
3. **Inconsistency risk**: Canonical and derived data can get out of sync
4. **API bloat**: Unnecessarily large response payloads
5. **Client capability**: Modern clients (including LLMs) can compute derived values

## Common Patterns

### Pattern 1: Status Booleans

```python
# BAD: Both status enum and derived boolean
class StatusResult(BaseModel):
    status: Status  # enum: COMPLETED, SKIPPED, FAILED, NONE
    completed: bool  # Just status == Status.COMPLETED

# GOOD: Only canonical status
class StatusResult(BaseModel):
    status: Status  # Clients compute: completed = (status == Status.COMPLETED)
```

**Rationale**: The `completed` field is redundant - it's just a boolean projection of the enum value.

### Pattern 2: Formatted Dates

```python
# BAD: Both machine and human formats
class LogEntry(BaseModel):
    date: str  # YYYY-MM-DD format
    formatted_date: str  # "Jan 15, 2023" - derived from date

# GOOD: Only canonical format
class LogEntry(BaseModel):
    date: str  # YYYY-MM-DD format - clients format as needed
```

**Rationale**: Date formatting is presentation logic. APIs should return canonical formats (ISO 8601, YYYY-MM-DD) and let clients format for display.

### Pattern 3: Computed Aggregations

```python
# BAD: Both items and derived count
class ItemsResult(BaseModel):
    items: list[Item]
    total_count: int  # Just len(items)

# GOOD: Only items (unless paginated)
class ItemsResult(BaseModel):
    items: list[Item]  # Clients compute: count = len(items)

# EXCEPTION: Paginated results where total_count != len(items)
class PaginatedResult(BaseModel):
    items: list[Item]  # Current page only
    total_count: int   # Total across all pages - NOT redundant
    page: int
    page_size: int
```

**Rationale**: If all items are returned, count is redundant. Only include `total_count` when it provides information not available from the items (e.g., pagination).

### Pattern 4: Intermediate Conversions

```python
# BAD: Unnecessary type conversions
# API returns: HabitStatus(date: str)  # YYYY-MM-DD
# Client converts: HabitStatusResponse(date: datetime.date)
# Tools convert back: format_date_yyyy_mm_dd(status.date) -> str

# GOOD: Keep API's canonical format
# API returns: HabitStatus(date: str)  # YYYY-MM-DD
# Client uses: date string directly
```

**Rationale**: Converting string → date object → string is pointless. Use the API's native format unless there's a compelling reason to convert.

## Detection Strategy

**Primary Method**: Manual code reading - understand the domain model and identify which fields are derived from others.

**Why automation is insufficient**: Determining if a field is "computed" vs "independent" requires understanding:

- The semantic relationship between fields (is `completed` derived from `status`?)
- Whether the computation is trivial (client-side) vs expensive (server-side required)
- Business rules and domain logic (when is a computed field justified?)
- Pagination and data availability (is `total_count` redundant or necessary?)

**Discovery aids** (candidates for manual review):

## Detection Patterns

```bash
# Find potential boolean flags that might be derived from enums
rg --type py -B3 -A3 "status.*:.*Status" | rg "completed.*:.*bool"

# Find formatted date fields alongside canonical dates
rg --type py -B2 -A2 "date.*:.*str" | rg "formatted"

# Find count fields alongside list fields
rg --type py -B2 -A2 "items.*:.*list" | rg "count.*:.*int"

# Find unnecessary type conversions (convert then convert back)
rg --type py "format_date.*\(.*\.date\)"
```

## Fix Strategy

1. **Identify canonical data**: Which field contains the source of truth?
2. **Remove derived fields**: Delete fields that can be computed from canonical data
3. **Update model constructors**: Remove parameters for derived fields
4. **Update client code**: Let clients compute derived values when needed
5. **Document format**: Ensure API documentation specifies canonical formats

## Examples from Codebase

### Example 1: Habitify Status (✅ Fixed)

**Before**:

```python
class StatusResult(BaseModel):
    status: Status
    date: str
    formatted_date: str  # Derived: format_date_human(date)
    completed: bool      # Derived: status == Status.COMPLETED

return StatusResult(
    status=status,
    date=date_str,
    formatted_date=format_date_human(date_str),
    completed=(status == Status.COMPLETED)
)
```

**After**:

```python
class StatusResult(BaseModel):
    status: Status
    date: str  # YYYY-MM-DD format

return StatusResult(status=status, date=date_str)
```

**Impact**: Reduced response size, removed 2 computed fields, clients format/interpret as needed.

## When Computed Fields ARE Acceptable

1. **Expensive computations**: If computing the value requires significant processing
2. **Server-side data**: If computation requires data not available to clients
3. **Backward compatibility**: When removing would break existing clients (use deprecation)
4. **Pagination metadata**: `total_count` when returning partial results
5. **Derived measurements**: When the computation involves domain logic (e.g., "days_until_due" from a complex business rule)

## References

- [API Design: Avoid Denormalization](https://apisyouwonthate.com/blog/guessing-api-http-status-codes)
- [Pydantic Computed Fields](https://docs.pydantic.dev/latest/concepts/computed_fields/) - Use sparingly, only when truly needed
