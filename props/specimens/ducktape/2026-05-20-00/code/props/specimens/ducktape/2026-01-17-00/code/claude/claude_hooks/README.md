# Claude Code Hooks Library

A type-safe, developer-friendly library for building Claude Code hooks with automatic JSON I/O handling, XDG directory support, and structured action APIs.

Hooks load YAML configuration from XDG config directory: `~/.config/adgn-claude-hooks/settings.yaml`

```yaml
precommit_autofixer:
  enabled: true
  timeout_seconds: 30
  tools:
    - Edit
    - MultiEdit
    - Write

my_hook:
  enabled: true
  custom_setting: value
```

Logs also go to XDG-compliant paths (e.g. `~/.local/state/claude-hooks/hookname.log`).

## Create a Simple Hook

```python
from claude_hooks.base import PostToolUseHook
from claude_hooks.inputs import PostToolInput, HookContext
from claude_hooks.actions import PostToolAction, PostToolContinue, PostToolFeedbackToClaude

class MyHook(PostToolUseHook):
    def __init__(self):
        super().__init__("my_hook")

    def execute(self, hook_input: PostToolInput, context: HookContext) -> PostToolAction:
        if hook_input.tool_name == "Write":
            return PostToolFeedbackToClaude(feedback_to_claude="File written!")
        return PostToolContinue()

if __name__ == '__main__':
    MyHook().run_hook()
```

## Built-in Hooks

### Pre-commit Autofixer

Automatically runs pre-commit autofix on files Claude modifies:

```bash
# ~/.claude/settings.json
{
  "PostToolUse": [
    {
      "matcher": "Edit|MultiEdit|Write",
      "hooks": [
        {
          "type": "command",
          "command": "python /path/to/scripts/autofixer.py",
          "timeout": 30
        }
      ]
    }
  ]
}
```

## Examples

See `docs/` for detailed specs:

- `precommit_autofix.md` - Pre-commit autofix integration
- `lint_enforcer_spec.md` - Lint violation tracking
- `custom_llm_triggers_spec.md` - Pattern-based interventions
