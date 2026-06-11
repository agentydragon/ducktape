# Python Style Guide

## Core Principles

**Simplicity over abstraction.** If there's no state, it's a function, not a class. If there's no shared behavior, don't create a base class. If a directory only has 2-3 files, flatten it.

**Question every abstraction.** "Is this interface actually useful?" If not, delete it.

**Colocate related code.** Things that change together live together.

## Python Style

- **Type hints everywhere**: `def parse_foo(data: Input) -> Output:`
- **Modern Python**: Use `|` for unions, walrus operator (`:=`), `match` statements
- **Target**: Python 3.11+
- **Pydantic for data models**: Use `BaseModel` for structured data
- **Functions over classes**: Only use classes when you have state to manage
- **Minimal docstrings**: Don't write docstrings that just restate the function name, parameters, or types. Only add docstrings when they provide information not obvious from the signature.
- **No duplication**: Extract constants/variables rather than repeat literals. If a string appears twice, make it a constant.
- **StrEnums over raw strings**: For well-known constant sets, use `StrEnum` for type safety and autocomplete.
- **Constants live where they're used**: Shared constants go in shared modules. Constants used by only one module stay in that module—no `constants.py` dumping ground.
- **Top-level imports**: No deferred imports inside functions unless truly justified (e.g., extremely heavy dependencies causing startup delays).
- **Prefer non-nullable**: Use Pydantic defaults (`Field(default_factory=list)`) over optional fields that require `or []` fallbacks everywhere.
- **Make invalid state unrepresentable**: Design types so illegal combinations can't be constructed.
- **Inline trivial locals**: Don't create intermediate variables just to hold a value used once.
- **Walrus operator**: Use `:=` when assigning and checking in sequence (e.g., `if (x := get_x()):` instead of `x = get_x(); if x:`).
- **Simple string ops over regex**: Don't use `re.compile()` for simple substring checks—use `str.lower()` + `in`.
- **Iterate correctly**: Use `.values()` not `.items()` when key is unused. Use `advance=len(items)` not a loop calling `advance(1)`.
- **Remove unused data**: Don't store tuple elements or dict keys that are never read.
- **Use framework features**: Prefer built-in validation (e.g., typer `exists=True`) over manual checks.
- **Use existing methods**: Don't reimplement pagination/batching when the client class already provides it.

## Error Handling

- **Don't silently swallow exceptions**: If catching exceptions, at minimum log them. Bare `except: return None` hides bugs.
- **Don't wrap bugs in try/except**: If an error represents a programming bug (e.g., plan collision), let it crash—don't catch and return early.
- **No defensive early returns for no-ops**: Don't add `if not items: return` when the subsequent loop would just be a no-op anyway.

## Module Organization

- **Avoid junk drawer names**: No `core.py`, `utils.py`, `constants.py`. Name modules by what they do.
- **Separate concerns**: Domain logic (Plan, Action) and view/display code (display_plan, summarize_plan) belong in separate modules.
- **Name by purpose**: `plan.py` + `plan_display.py` is better than `core.py` containing both.

## What to Avoid

**Over-engineering**: Don't add manual mappings when stdlib already handles it.

**Empty abstractions**: Don't create functions that just return `True` because "it's part of the interface."

**Deep hierarchies**: Don't make `parsers/base.py`, `parsers/constants.py`, `parsers/__init__.py` when you can just have `parsers.py`.

**Premature separation**: Don't split things by "is it a constant vs function vs class" - organize by domain/feature.

**Classes without state**: If `__init__` just does `self.x = SomeClass()`, you don't need a class.

**Useless comments**: Don't write comments that just restate what the next line does (e.g., `# Connect to Gmail` before `client = get_gmail_client()`).

## Testing

- Tests go in `tests/` directory
- Name: `test_<module>.py`
- Use pytest
- Test the public interface, not implementation details

## When to Refactor

**Do refactor when**:

- You find unused code → delete it immediately
- Multiple files doing the same thing → merge them
- A class has no state → make it functions
- An interface isn't being used → remove it
- A subdirectory has <3 files → flatten it

**Don't refactor when**:

- "It might be useful later"
- "It's more proper OOP this way"
- "It follows a pattern I learned"

## Questions to Ask Yourself

Before adding complexity:

- "Does this class have state?" → If no, use functions
- "Is this abstraction actually used differently?" → If no, remove it
- "Could this be simpler?" → Usually yes
- "Am I organizing by role (classes.py, functions.py) or by purpose?" → Purpose wins

## Summary

Write simple, direct Python. Use types. Delete dead code. Question abstractions. Functions over classes. Flat over nested. Purpose over taxonomy.

## Project-Specific Patterns

### File Organization

```
gmail_archiver/
  planners/           # Each planner file contains:
    foo.py           #   - Pydantic data model
                     #   - Parsing function(s)
                     #   - Planner class
  core.py            # Plan, PlannedAction, shared constants
  inbox.py           # Gmail API wrapper
  models.py          # GmailMessage
  date_patterns.py   # Shared regex patterns
```

### Data Models (Pydantic)

```python
class FooReceipt(BaseModel):
    amount: Decimal | None = None
    date: datetime | None = None
```

### Parsing Functions

If there's no state, just write functions:

```python
def parse_foo(email: GmailMessage) -> FooReceipt:
    # Do the work
    return FooReceipt(...)
```

NOT:

```python
class FooParser:
    def __init__(self):
        pass  # ← No state? Why is this a class?

    def parse(self, email: GmailMessage) -> FooReceipt:
        ...
```

### Planners

Planners coordinate the archiving logic. They can be classes since they have configuration (thresholds, names):

```python
class FooPlanner:
    """Archives foo emails older than N days."""

    name = "Foo emails"
    DAYS_THRESHOLD = 30

    def plan(self, inbox: GmailInbox) -> Plan:
        messages = inbox.fetch_messages("label:foo label:INBOX")
        plan = Plan(planner=self)

        for message in messages:
            parsed = parse_foo(message)
            # Decision logic here

        return plan
```

### Common Patterns

#### Date parsing from email headers

```python
try:
    dt = datetime.strptime(email.date, "%a, %d %b %Y %H:%M:%S %z")
    dt = dt.replace(tzinfo=None)
except (ValueError, AttributeError):
    dt = None
```

#### Regex extraction with optional fields

```python
amount = None
if match := AMOUNT_REGEX.search(body):
    with contextlib.suppress(ValueError):
        amount = Decimal(match.group(1).replace(",", ""))
```

#### Archive decision logic

```python
from gmail_archiver.core import LABEL_AUTO_CLEANED
from gmail_archiver.gmail_api_models import SystemLabel

if date >= cutoff_date:
    plan.add_action(
        message=message,
        labels_to_add=[],
        labels_to_remove=[],
        reason=f"Too recent (within {threshold} days)",
    )
else:
    plan.add_action(
        message=message,
        labels_to_add=[LABEL_AUTO_CLEANED],
        labels_to_remove=[SystemLabel.INBOX],
        reason=f"Old enough (> {threshold} days)",
    )
```
