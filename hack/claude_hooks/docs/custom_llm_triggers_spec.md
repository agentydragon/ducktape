# Custom LLM triggers

Specialized, context-aware interventions from processing the transcript by a LLM. Prevent common mistakes, give targeted guidance, etc.

Use any appropriate events, e.g.:

- `PreToolUse` ("you are starting a blocking process, set a timeout")
- `PostToolUse` ("suggestion: are you confused about your pwd?")
- `Stop` ("you still have a failing unit test")

## Trigger Rules

Trigger rules define:

- **Trigger of when to send to LLM**: always, regex, Python preficate
- **Action**: LLM could directly output an action possible for current hook
- **Severity levels**: Determines intervention type

## Concept Overview

The system analyzes:

- User prompts for potentially problematic patterns
- Tool commands before execution
- Conversation context for missing best practices

And provides appropriate interventions ranging from blocking dangerous operations to suggesting improvements.

## Examples

**General:**

- Suggest to organize if workspace is messy (e.g., many `.md`, debug files)
