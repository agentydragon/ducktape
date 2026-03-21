Inherits from root <../STYLE.md>. Only project-specific patterns below.

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

### Planners

Planners coordinate archiving logic. They can be classes (they have configuration: thresholds, names). Parsers are stateless functions, not classes.
