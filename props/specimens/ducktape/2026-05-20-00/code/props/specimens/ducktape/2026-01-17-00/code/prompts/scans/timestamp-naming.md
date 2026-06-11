# Scan: Timestamp Field Naming

## Context

@../shared-context.md

## Overview

Timestamp fields should use `_at` suffix (not `_ts`), and should prefer `updated_at` over `last_update_ts`.

## Pattern: `_ts` Suffix

### Bad Examples

```python
class Response(BaseModel):
    created_ts: datetime  # Inconsistent with common conventions
    last_update_ts: datetime  # Verbose, non-standard
    modified_ts: datetime
```

### Good Examples

```python
class Response(BaseModel):
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None  # Soft deletes
```

## Rationale

**Industry standard**: `_at` is the dominant convention:

- Rails ActiveRecord: `created_at`, `updated_at`
- Django: `created_at`, `updated_at` (in newer code)
- SQLAlchemy examples: `created_at`, `updated_at`
- Stripe API: `created`, but in docs referred to as "created at"
- GitHub API: `created_at`, `updated_at`

**Consistency**: Most codebases use `_at`, not `_ts`:

- PostgreSQL guides prefer `_at`
- Database migration tools default to `_at`

**Clarity**:

- `created_at` reads naturally ("created at [timestamp]")
- `created_ts` requires parsing ("created timestamp")

**Brevity**:

- `updated_at` vs `last_update_ts` (11 chars vs 14 chars)
- `deleted_at` vs `deletion_ts` (10 chars vs 12 chars)

## Common Timestamp Fields

| Purpose       | Recommended    | Avoid                                            |
| ------------- | -------------- | ------------------------------------------------ |
| Creation time | `created_at`   | `created_ts`, `creation_time`, `create_date`     |
| Last update   | `updated_at`   | `last_update_ts`, `modified_ts`, `last_modified` |
| Soft delete   | `deleted_at`   | `deleted_ts`, `deletion_time`                    |
| Published     | `published_at` | `publish_ts`, `publication_date`                 |
| Scheduled     | `scheduled_at` | `scheduled_ts`, `schedule_time`                  |

## Detection Strategy

**Primary Method**: Manual code reading to identify timestamp field naming inconsistencies.

**Why automation is insufficient**:

- Some `_ts` fields might be abbreviations for domain terms (not "timestamp")
- Need to understand if renaming requires database migration (breaking change)
- Context matters: is this legacy code during migration or new antipattern?

**Discovery aids** (usually accurate for this pattern):

```bash
# Find _ts suffix timestamp fields (likely candidates)
rg --type py '^\s+\w+_ts:\s*Mapped\[datetime\]'
rg --type py '^\s+\w+_ts:\s*datetime'

# Find verbose timestamp names
rg --type py 'last_update|last_modified|creation_time'
```

**Note**: This is one of the few patterns where automation has high accuracy (timestamp suffixes are fairly unambiguous).

## Fix Strategy

1. **Rename database columns** (requires migration):

   ```python
   # Before
   created_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))

   # After
   created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
   ```

2. **Update all references**:

   ```python
   # API models
   created_at: datetime  # was created_ts

   # Queries
   .order_by(Response.created_at.desc())  # was created_ts

   # Assignments
   record.updated_at = datetime.now(UTC)  # was last_update_ts
   ```

3. **Add backward compatibility if needed**:

   ```python
   class ResponseRecordModel(BaseModel):
       created_at: datetime

       @property
       def created_ts(self) -> datetime:
           """Deprecated: use created_at"""
           return self.created_at
   ```

## Special Cases

**OK to use `_ts` when**:

- External API requires it (match their naming)
- Legacy system integration (consistency with existing)
- Abbreviation is industry-standard for that domain

**Timestamp vs Date**:

- `*_at` for timestamps (includes time): `datetime`
- `*_date` for dates only (no time): `date`
- `*_time` for time only (no date): `time`

```python
# GOOD: Clear type from name
birth_date: date  # Date only
created_at: datetime  # Full timestamp
daily_start_time: time  # Time only

# BAD: Ambiguous
birth: datetime  # Date or datetime?
created: date  # Why date not datetime?
```

## Benefits

✅ **Consistency** - Matches 90% of modern codebases
✅ **Readability** - "created at" reads naturally
✅ **Brevity** - Shorter than verbose alternatives
✅ **IDE support** - Autocomplete recognizes common pattern
✅ **Onboarding** - New developers expect `_at` convention

## Examples from rspcache

```python
# ✗ BAD: Non-standard _ts suffix
class Response(Base):
    created_ts: Mapped[datetime]
    last_update_ts: Mapped[datetime]

# ✓ GOOD: Standard _at suffix
class Response(Base):
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

## References

- [Rails ActiveRecord Timestamps](https://guides.rubyonrails.org/active_record_basics.html#timestamps)
- [PostgreSQL Naming Conventions](https://wiki.postgresql.org/wiki/Don%27t_Do_This#Don.27t_use_timestamp_with_time_zone_columns_for_metadata)
- [Database Design Best Practices](https://www.vertabelo.com/blog/naming-conventions-in-database-modeling/)
