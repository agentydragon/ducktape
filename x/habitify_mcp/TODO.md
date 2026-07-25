# Habitify MCP — refactoring TODO

Verify each item is still wanted before acting.

## Open

- [ ] **Standardize error handling.** `habitify_client.py` still has 7 broad
      `except Exception` blocks and `cli.py` has 1. Catch specific httpx
      exceptions; define clear error-response patterns.
- [ ] **Simplify `_handle_error`** (`habitify_client.py`, ~58 lines): use an
      error-handler map, reduce nesting, extract message formatting.
- [ ] **Circular-import workarounds** in `utils/__init__.py` +
      `utils/habit_resolver.py` — re-check whether the split is still needed; if
      so, move habit resolution to its own module / dependency injection.
- [ ] **Date handling**: `utils/date_utils.py` exists; assess whether format
      conversions can be consolidated further.
- [ ] **Extract shared logging setup** (duplicated in `examples/`) into a
      `logging_config.py`.
