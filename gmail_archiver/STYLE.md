# gmail_archiver Style

Elaborations on the root <../../STYLE.md>. Do not duplicate rules already covered there.

## Module Organization

- **Separate concerns**: Domain logic (Plan, Action) and view/display code (display_plan, summarize_plan) belong in separate modules.
- **Name by purpose**: `plan.py` + `plan_display.py` is better than `core.py` containing both.

## Project-Specific Patterns

### File Organization

```
gmail_archiver/
  planners/           # Each planner file contains:
    foo.py           #   - Pydantic data model
                     #   - Parsing function(s)
                     #   - Planner class
  plan.py            # Plan, PlannedAction, Planner protocol, LABEL_AUTO_CLEANED
  plan_display.py    # Plan rendering / display helpers
  inbox.py           # Cached Gmail access (GmailInbox)
  gmail_client.py    # Gmail API wrapper
  models.py          # Email
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
from gmail_archiver.plan import LABEL_AUTO_CLEANED
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
