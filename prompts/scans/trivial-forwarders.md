# Scan: Trivial Forwarder Functions

## Context
@../shared-context.md

## Pattern Description

Functions that do nothing but forward to another function with identical or trivially transformed arguments.

## Examples of Antipattern

```python
# BAD: Trivial wrapper around model_dump
def dump_response(value: OpenAIResponse | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json")

# BAD: Just forwarding to another module
def extract_text_from_openai_response(response: ResponsesResult) -> str:
    return first_assistant_text(response)
```

## Detection Strategy

### AST Analysis
```python
import ast

class TrivialForwarderDetector(ast.NodeVisitor):
    def visit_FunctionDef(self, node):
        # Check if function body is:
        # 1. Single return statement
        # 2. That calls another function
        # 3. With same/trivially mapped arguments
        if len(node.body) == 1 and isinstance(node.body[0], ast.Return):
            ret = node.body[0]
            if isinstance(ret.value, ast.Call):
                # Analyze if it's just forwarding
                pass
```

### Grep Pattern
```bash
# Find functions with single return statement
rg --type py -U "def \w+\([^)]*\):[^\n]*\n\s+return \w+\("
```

## Fix Strategy

1. **Remove the wrapper**: Migrate all callers to use the underlying function directly
2. **Update imports**: Change import statements at call sites
3. **Verify**: Run mypy to ensure types still work

## Common False Positives

- Functions that add validation before forwarding
- Functions that transform error types
- Functions that add logging/metrics
- Functions that provide backward compatibility during migration (temporary)

## Example Fix

```python
# Before:
from module import dump_response
result = dump_response(snapshot.response)

# After:
result = snapshot.response.model_dump(mode="json") if snapshot.response else None
```

## Validation

```bash
# Check no references remain to removed function
rg "dump_response|extract_text_from_openai_response"

# Run type checker
mypy path/to/modified/files.py
```
