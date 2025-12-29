# rfc3987-syntax import speed optimization

Proposed patch for https://github.com/willynilly/rfc3987-syntax to reduce import time from ~1 second to ~165ms (6x speedup).

## Problem

The original `syntax_helpers.py` creates 40 separate Lark parser instances at import time - one main parser plus 38 individual validators via `make_syntax_validator()`. Each `Lark()` call parses the grammar and builds an Earley parser, which is expensive.

## Solution

Use a single shared parser with all start rules instead of creating separate parsers:

```python
# Before: 40 separate Lark() calls
syntax_parser = Lark(grammar, start=["iri", "iri_reference", "absolute_iri"], ...)
# + 38 more Lark() calls in make_syntax_validator()

# After: 1 shared parser with all start rules
ALL_START_RULES = ["iri", "iri_reference", ..., "port"]  # 38 rules
syntax_parser = Lark(grammar, start=ALL_START_RULES, parser="earley")

def make_syntax_validator(rule_name):
    def syntax_validator(text):
        syntax_parser.parse(text, start=rule_name)  # reuse shared parser
        ...
```

## Benchmark results

| Version | Import Time | syntax_helpers |
|---------|-------------|----------------|
| Original (40 parsers) | ~1005ms | ~912ms |
| Optimized (1 parser) | ~165ms | ~62ms |
| **Speedup** | **6.1x** | **14.7x** |

## Notes

- LALR parser is not compatible with the RFC 3987 grammar due to Reduce/Reduce conflicts (inherent ambiguity in URI syntax requiring backtracking)
- The optimization maintains full API compatibility
- All existing validators continue to work identically
