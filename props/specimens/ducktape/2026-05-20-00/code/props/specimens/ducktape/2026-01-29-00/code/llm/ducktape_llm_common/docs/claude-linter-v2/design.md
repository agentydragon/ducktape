# Claude Linter v2 Design

## Executive Summary

Claude Linter v2 is a monolithic code quality and permission management system for Claude Code, providing:

- Python predicate-based access control with session and repo-level rules
- Hard blocks for dangerous patterns (bare except, hasattr/getattr)
- Selective autofixing based on hook type (full fix for Write, formatting-only for Edit)
- LLM-based code analysis for subtle issues
- Contextual guidance system for teaching best practices
- MCP server for permission queries and async requests

## Architecture

### Monolithic Design

Single binary with internal modularity:

```
claude-linter/
├── cli.py                    # Entry point
├── config/                   # Configuration management
├── access/                   # Access control with predicates
├── linters/                  # Language-specific linters
├── hooks/                    # Hook handlers (pre/post/stop)
├── session/                  # Session state management
└── mcp/                      # MCP server
```

Benefits:

- Single process (no IPC overhead)
- Shared memory and state
- Unified configuration
- Simpler deployment

## Core Features

### 1. Python Predicate Access Control

Powerful rule system using Python expressions:

```python
# Session rules (temporary)
claude-linter session allow 'Edit("src/**/*.py") and not Edit("**/*test.py")' --expires 2h

# Repo rules (persistent in .claude-linter.toml)
[[repo_rules]]
predicate = 'Edit("src/core/**") or Edit("LICENSE")'
action = "block"
message = "Core files are read-only"
```

Built-in predicates:

- `Edit()`, `Write()`, `Read()`, `Bash()` - Tool matchers
- `safe_git_commands()` - Allow safe git operations only
- `is_test()`, `is_prod()` - Path helpers
- Boolean operators: `and`, `or`, `not`

### 2. Hook Behavior

#### PreToolUse

1. Check access control (fastest fail)
2. Run hard blocks (bare except, hasattr)
3. Check format issues (inform only)

#### PostToolUse

- **Write**: Full autofix all categories
- **Edit/MultiEdit**: Selective autofix (formatting only by default)
- Report violations
- Inject permissions/guidance

#### Stop

- Quality gate: Block if unfixed errors
- Cleanup questionnaire

### 3. Diff-Based Intelligence

Analyze only changed code with context awareness:

- **In-diff violations**: Block (Claude just added these)
- **Near-diff violations**: Warn (context issues)
- **Out-of-diff violations**: Report only
- **Serial change detection**: More lenient for sequential fixes

### 4. Hard-Blocked Patterns

AST-based blocking for Python:

```toml
[python.hard_blocks]
bare_except = true      # Blocks except: without type
getattr_setattr = true  # Blocks hasattr/getattr/setattr
eval_exec = true        # Blocks eval() and exec()
```

### 5. LLM-Based Analysis

Optional AI-powered checks:

```toml
[llm_analysis]
enabled = true
model = "gpt-4o-mini"
check_types = ["error_hiding", "security_issues"]
daily_cost_limit = 5.00
```

### 6. Contextual Guidance

Detect patterns and inject timely advice:

- Test writing → pytest best practices
- API client code → timeout/retry guidance
- Security patterns → security reminders

### 7. Task Profiles

Pre-approved permission sets:

```toml
[[profiles]]
name = "refactoring"
predicate = 'Edit("**/*.py") and Bash("git:*") and Bash("pytest:*")'
duration = "4h"
```

## Configuration Schema

```toml
# .claude-linter.toml
version = "2.0"

# Path-based access control
[[access_control]]
paths = ["production/**", "*.prod.py"]
tools = ["Write", "Edit", "MultiEdit"]
action = "block"

# Repo-wide rules using predicates
[[repo_rules]]
predicate = 'Edit("src/core/**")'
action = "block"

# Language-specific
[python]
tools = ["ruff", "mypy"]
hard_blocks.bare_except = true

# Hook behavior
[hooks.post_edit]
auto_fix = true
autofix_categories = ["formatting"]  # Only safe fixes

# Test file handling
[[test_patterns]]
pattern = "**/test_*.py"
relaxed_rules = ["bare_except", "type_checking"]
```

## CLI Interface

```bash
# Install hooks in Claude Code (one-time setup)
cl2 install                           # Installs all 5 hook types
cl2 install --dry-run                # Preview what would be installed

# Hook mode (called automatically by Claude Code)
cl2 hook                             # No --type needed, uses hook_event_name from request

# Session management (auto-detects sessions in current directory)
cl2 session allow 'Edit("src/**")' --expires 2h
cl2 session deny 'Write("/etc/*")'
cl2 session forbid 'Bash("sudo *")'  # User-friendly alias for deny
cl2 session list                     # Show sessions in current dir
cl2 session list --all               # Show all sessions

# Profile management (TODO)
cl2 profile list
cl2 profile activate refactoring

# Direct usage (TODO)
cl2 check src/main.py
cl2 fix src/main.py --categories formatting
```

### Session ID Management

Since Claude Code doesn't display session IDs and multiple instances can run in parallel:

1. **Hook calls**: Session ID comes from hook request JSON
2. **CLI commands**: Must infer which session(s) to affect
3. **Context-based selection**: Use working directory to narrow down sessions

```bash
# Commands affect ALL sessions in current directory
claude-linter session allow 'Edit("src/**")'  # Applies to all sessions in $PWD

# Or see all sessions and choose
claude-linter session list
# Output:
# Sessions in /home/user/project:
#   abc123 - last seen 2m ago
#   def456 - last seen 5m ago
# Sessions in other directories:
#   xyz789 - /home/user/other - last seen 1h ago

# Apply to specific session
claude-linter session allow 'Edit("src/**")' --session abc123

# Apply to all sessions in a directory
claude-linter session allow 'Edit("src/**")' --dir /home/user/project
```

When Claude is blocked, the error message is targeted at the LLM:

```
Error: Permission denied to edit src/core/security.py

To request an override, ask the user to run:
claude-linter session allow 'Edit("src/core/security.py")' --session abc123
```

## MCP Server

Allows Claude to query and request permissions:

```typescript
{
  "name": "claude-linter-permissions",
  "tools": [
    {
      "name": "query_permissions",
      "description": "Check what permissions I have",
      "input_schema": {
        "properties": {
          "tool": { "enum": ["Write", "Edit", "Bash", "Read"] },
          "path": { "type": "string" }
        }
      }
    },
    {
      "name": "request_permission",
      "description": "Request permissions for operations",
      "input_schema": {
        "properties": {
          "operations": { "type": "array" },
          "duration": { "type": "string" }
        }
      }
    }
  ]
}
```

## Key Innovations

### 1. Permission Communication

Post-hook injection informs Claude of active permissions:

```
foo.py written OK with whitespace fixes.

FYI: You have blanket approval for:
- Any awk commands until 13:07
- Git commands an LLM judges as safe
- Editing files matching src/**/*.py until 14:30
```

### 2. Checklist-Based Overrides

For hasattr/getattr usage, Claude must complete a detailed checklist explaining why it's necessary.

### 3. Safe Git Commands

Predicate function that allows safe git operations while blocking force pushes, history rewrites, etc.

### 4. Stop Hook Questionnaire

Forces cleanup reflection:

- List temporary files created
- Check for duplicate functionality
- Remove debug code
- Document accomplishments

## Implementation Strategy

### Parallel Implementation

Claude Linter v2 will be implemented as a separate package/command to avoid disrupting the existing v1:

```
ducktape_llm_common/
├── claude_linter/          # Existing v1 (unchanged)
├── claude_linter_v2/       # New v2 implementation
│   ├── __init__.py
│   ├── cli.py
│   ├── config/
│   ├── access/
│   └── ...
```

Command names:

- v1: `claude-linter` (unchanged)
- v2: `claude-linter-v2` or `cl2`

This allows:

- Gradual migration without breaking existing workflows
- A/B testing between versions
- Easy rollback if needed
- Eventually deprecate v1 once v2 is stable

## Implementation Status (Updated: Jan 2025)

### ✅ Completed

1. **Phase 1**: Core framework + config system ✅
   - CLI with `cl2` command
   - Pydantic-based configuration models
   - Hook handler infrastructure

2. **Phase 2**: Python predicate access control ✅
   - Unrestricted Python eval for predicates
   - Session management with per-session files
   - Built-in predicates (Edit, Write, etc.)
   - "Most restrictive wins" rule evaluation

3. **Phase 3**: Python hard blocks ✅
   - AST analyzer for bare except
   - Blocks hasattr/getattr/setattr usage
   - Detects barrel **init**.py patterns
   - Integrates with pre-hook blocking

4. **Phase 4**: Selective autofix by hook type ✅
   - Full autofix for Write tool
   - Formatting-only for Edit/MultiEdit
   - Ruff integration with critical rules
   - FYI pattern for post-hook notifications

5. **Phase 5**: Session tracking ✅
   - Per-session file storage
   - Session commands: allow, deny, forbid, list
   - Directory-based session inference

6. **Install command** ✅
   - `cl2 install` configures all 5 hook types
   - Single command handles all hooks via `hook_event_name`
   - Forward compatibility (unknown hooks → no-op)

### 🚧 TODO (High Priority)

- **Stop hook quality gate**: Block if unfixed errors remain
- **Diff-based intelligence**: In-diff vs near-diff vs out-of-diff

### 📋 TODO (Medium Priority)

- **Task profiles**: Pre-configured permission sets
- **safe_git_commands predicate**: Built-in safety checks
- **Duration parsing**: Support "2h", "30m" formats
- **Modular config**: Refactor to `[python.bare_except]` style
- **Direct file checking**: Make `cl2 check` work

### 🔮 TODO (Low Priority)

- **Phase 6**: MCP server for permission queries
- **Phase 7**: LLM analysis integration
- **Phase 8**: Contextual guidance system
- **Predicate sandboxing**: Security for untrusted predicates

## Design Principles

- **Most restrictive wins**: Any block prevents operation
- **Fail fast**: Access control before expensive checks
- **Progressive enhancement**: Start with core, add features
- **Clear communication**: Tell Claude what it can/cannot do
- **Monolithic simplicity**: One tool, one config, one process
