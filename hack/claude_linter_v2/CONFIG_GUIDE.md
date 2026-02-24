# Claude Linter v2 Configuration Guide

This directory contains example configuration files for Claude Linter v2, showing different use cases and all available options.

## Configuration Files

### 1. `example-config.toml` - Complete Reference

A comprehensive configuration file that lists **every available option** with detailed comments explaining:

- What each option does
- Default values
- Valid choices
- Example usage

Use this as a reference when creating your own configuration.

### 2. `example-config-minimal.toml` - Quick Start

A minimal configuration for getting started quickly:

- Blocks access to sensitive files (.env, node_modules)
- Allows Python editing
- Enables auto-formatting
- Sets up quality gates

Perfect for local development environments.

### 3. `example-config-production.toml` - Production Lockdown

A strict configuration for production environments:

- Default-deny access control
- Explicitly allowed paths only
- No shell commands except specific safe ones
- No auto-fix to prevent unexpected changes
- Audit logging enabled

Use this as a starting point for secure production deployments.

## Key Configuration Sections

### Access Control

```toml
[[access_control]]
path_pattern = "**/.env"
action = "deny"  # or "allow" or "warn"
message = "Optional explanation"
```

### Predicate Rules

```toml
[[repo_rules]]
predicate = "Edit('**/*.py') and not Edit('**/migrations/**')"
action = "allow"
reason = "Python files can be edited except migrations"
```

### Python Settings

```toml
[python.hard_blocks]
bare_except = true      # Block bare except:
getattr_setattr = true  # Block hasattr/getattr/setattr
barrel_init = true      # Block barrel __init__.py patterns
```

### Hook Behaviors

```toml
[hooks.post]
auto_fix = true
autofix_categories = ["formatting", "imports"]  # or ["all"]

[hooks.stop]
quality_gate = true  # Prevent stopping with errors
```

## Using a Configuration

1. **Create your config file** based on one of the examples:

   ```bash
   cp example-config-minimal.toml ~/.claude-linter.toml
   ```

2. **Edit the configuration** to match your needs

3. **Use with Claude Linter**:

   ```bash
   # Specify config file
   cl2 check --config ~/.claude-linter.toml

   # Or set environment variable
   export CLAUDE_LINTER_CONFIG=~/.claude-linter.toml
   ```

## Configuration Precedence

1. Command-line arguments (highest priority)
2. Environment variables
3. Config file specified via `--config`
4. `.claude-linter.toml` in current directory
5. `~/.claude-linter.toml` in home directory
6. Default values (lowest priority)

## Common Patterns

### Allow Specific Tools

```toml
# Only allow reading
[[repo_rules]]
predicate = "Read('**')"
action = "allow"

# Git operations
[[repo_rules]]
predicate = "Bash('git status') or Bash('git diff')"
action = "allow"
```

### Time-based Rules

```toml
[[repo_rules]]
predicate = "Write('logs/**') and time.hour >= 9 and time.hour <= 17"
action = "allow"
reason = "Log writes only during business hours"
```

### File Size Limits

```toml
[[repo_rules]]
predicate = "Write(path) and file_size(path) < 1048576"
action = "allow"
reason = "Only allow writing files smaller than 1MB"
```

## Tips

1. **Start with minimal config** and add restrictions as needed
2. **Use `warn` action** to test rules before enforcing with `deny`
3. **Be specific with path patterns** - use `**/` for recursive matching
4. **Test your predicates** with `cl2 session test-predicate`
5. **Use profiles** for common task combinations

## Debugging Configuration

To see how your configuration is being interpreted:

```bash
# Show effective configuration
cl2 config show

# Test a specific predicate
cl2 session test-predicate "Edit('src/main.py')"

# Check what's allowed in current session
cl2 session show
```
