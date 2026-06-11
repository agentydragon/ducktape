# Habitify MCP Server - Refactoring Plan

## Overview

This document outlines opportunities for refactoring and improvements in the Habitify MCP Server codebase.

## High Priority Refactoring

### 1. **Standardize Error Handling**

Currently using broad `except Exception` catches throughout.

**Files affected:**

- `habitify_mcp_server/habitify_client.py` (7 occurrences)
- `habitify_mcp_server/tools.py` (2 occurrences)

**Solution:**

- Create specific exception handlers for httpx exceptions
- Define clear error response patterns
- Use error handler mapping for cleaner code

## Medium Priority Refactoring

### 2. **Simplify `_handle_error` in habitify_client.py (58 lines)**

**Solution:**

- Use error handler mapping pattern
- Reduce nesting levels
- Extract error message formatting

### 3. **Resolve Circular Import Issues**

The codebase has workarounds for circular imports.

**Files affected:**

- `habitify_mcp_server/utils/__init__.py`
- `habitify_mcp_server/utils/habit_resolver.py`

**Solution:**

- Move habit resolution logic to a separate module
- Consider dependency injection pattern
- Restructure module dependencies

## Low Priority Improvements

### 4. **Standardize Type Hints**

- Use `str | None` instead of `Optional[str]` (Python 3.10+)
- Add missing return type annotations
- Be consistent with type imports

### 5. **Improve Date Handling**

- Create a centralized date handling utility
- Standardize on fewer date formats
- Reduce conversions between string/datetime/date

### 6. **Extract Common Patterns**

#### Logging Setup (duplicated in examples/)

```python
# Create habitify_mcp_server/logging_config.py
def setup_logging(name: str, level: str = "INFO") -> Logger:
    ...
```

## Notes

- All refactoring should maintain backward compatibility
- Add tests for any new utilities created
- Update documentation as needed
- Consider performance implications of changes
