# Claude Linter v2 Documentation

Claude Linter v2 is a unified code quality and permission management system for Claude Code, providing AST-based Python checks, integration with ruff, and fine-grained access control.

## Documentation Index

### Core Documentation

- **[Design Document](design.md)** - Overall architecture and implementation phases
- **[Configuration Guide](configuration.md)** - How to configure checks and rules
- **[Test Scenarios](test-scenarios.md)** - Examples of how the linter behaves in different situations

### Technical Deep Dives

- **[Predicate Design](predicate-design.md)** - General predicate system for access control
- **[Python Predicate Design](python-predicate-design.md)** - Python-specific predicate implementation
- **[Diff Intelligence Report](diff-intelligence-report.md)** - Research on implementing diff-based violation detection

## Quick Start

### Installation

```bash
# Install claude-linter-v2 globally
pip install /path/to/ducktape_llm_common

# Install hooks in Claude Code
cl2 install
```

### Basic Configuration

Create a `.claude-linter.toml` file in your project:

```toml
version = "2.0"

# Python checks
[python.bare_except]
enabled = true
message = "Bare except clauses hide errors"

[python.hasattr]
enabled = true
message = "Use isinstance() instead of hasattr()"

# Ruff rules
[ruff.E722]
enabled = true

[ruff.S113]
enabled = true
severity = "error"

# Hooks
[hooks.stop]
quality_gate = true  # Block session end if unfixed errors
```

### CLI Commands

```bash
# Check files directly
cl2 check file.py
cl2 check src/ --fix
cl2 check --json

# Session management
cl2 session list
cl2 session allow 'Edit("**/*.py")' --duration 2h
cl2 session forbid 'Write("production/**")'

# Fix files
cl2 fix src/ --categories formatting,imports
```

## Key Features

### 1. Modular Configuration

Each check has its own configuration section with standard fields:

- `enabled` - Whether the check is active
- `message` - Custom error message
- `severity` - error, warning, or info
- `autofix` - Whether the issue can be auto-fixed

### 2. Python AST Checks

Built-in checks for common Python anti-patterns:

- Bare except clauses
- hasattr/getattr/setattr usage
- Barrel **init**.py files

### 3. Ruff Integration

Seamlessly integrates with ruff for additional Python linting with configurable rules.

### 4. Access Control

Fine-grained permission system with:

- Path-based rules
- Python predicate expressions
- Session-specific permissions
- "Most restrictive wins" precedence

### 5. Hook Integration

Integrates with Claude Code's hook system:

- **PreToolUse**: Block problematic code before execution
- **PostToolUse**: Auto-fix formatting issues
- **Stop**: Quality gate to ensure clean code before session end

## Architecture Overview

The system consists of several key components:

1. **Config System** - Modular TOML-based configuration
2. **AST Analyzer** - Python AST-based code analysis
3. **Ruff Integration** - External linter integration
4. **Access Control** - Predicate-based permission system
5. **Session Management** - Per-session rule tracking
6. **Hook Handlers** - Claude Code integration points
7. **CLI Interface** - Direct interaction and management

## Future Enhancements

- Diff-based intelligence (in-diff vs out-of-diff violations)
- Task profiles for common workflows
- LLM-based analysis for complex patterns
- MCP server for permission queries
- Contextual fix suggestions
