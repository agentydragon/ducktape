# flake8-early-bailout

A flake8 plugin to encourage early bailout/guard clause patterns.

## Purpose

Detects deeply nested code that could be flattened using early returns, continues, or breaks. Encourages using vertical space (unlimited) rather than horizontal space (limited).

## Example

```python
# Bad - EB100 triggered
def process(data):
    if data:
        result = transform(data)
        final = finalize(result)
        return final
    else:
        return None

# Good
def process(data):
    if not data:
        return None
    result = transform(data)
    final = finalize(result)
    return final

# Bad - EB101 triggered (nested ifs)
def check(x):
    if x > 0:
        if x < 100:
            return "valid"

# Good
def check(x):
    if x <= 0:
        return None
    if x >= 100:
        return None
    return "valid"
```

## Installation

```bash
pip install flake8-early-bailout
```

Or for development:

```bash
pip install -e .
```

## Usage

```bash
# Check specific file
flake8 --select=EB file.py

# Check entire project
flake8 --select=EB .
```

## Error Codes

- **EB100**: If/else block should be inverted (short path in else, long path in if)
- **EB101**: Nested if statements creating rightward drift
