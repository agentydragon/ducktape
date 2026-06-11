# flake8-early-bailout

Flake8 plugin that detects deeply nested code that could use early returns/guard clauses.

## Error Codes

- **EB100**: If/else block should be inverted (short path in else, long path in if)
- **EB101**: Nested if statements creating rightward drift

## Usage

```bash
flake8 --select=EB file.py
```
