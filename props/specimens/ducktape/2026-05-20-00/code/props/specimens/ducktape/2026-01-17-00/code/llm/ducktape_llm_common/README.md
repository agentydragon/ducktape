# Ducktape LLM Common

A comprehensive shared Python package providing utilities and linters for LLM development workflows.

## Overview

`ducktape-llm-common` implements the common automation referenced by standard operating procedures in LLM development workflows. It provides:

- **Linters**: Enforce coding standards, validate custom URL formats and metadata files
- **Utilities**: Version management and common validation functions
- **Templates**: Quick-start structures for investigations and tasks

## Installation

See the repository root AGENTS.md for the standard Bazel workflow.

```bash
bazel build //llm/ducktape_llm_common/...
bazel test //llm/ducktape_llm_common/...
bazel build --config=check //llm/ducktape_llm_common/...  # lint + typecheck
```

### Requirements

- Python 3.13+

## Quick Start

### Console Scripts Available

- `claude-linter` - Unified linter for Claude Code hooks (pre/post/check modes)
- `check-work-urls` - Validate work URLs in markdown files
- `check-task-metadata` - Validate METADATA.yaml files

### Using Linters

The package provides command-line linters that can be used standalone or with pre-commit:

```bash
# Claude Code hook modes
claude-linter pre   # Run pre-hook (blocks non-fixable violations)
claude-linter post  # Run post-hook (auto-fixes violations)
claude-linter check # Manual check mode for developers

# Check work tracking URLs in your project
check-work-urls .

# Validate task metadata files
check-task-metadata .
```

For detailed documentation on the Claude linter, see <docs/linters/claude-linter.md>.

### Using Templates

Create standard project structures quickly:

```python
from ducktape_llm_common.templates import (
    create_investigation_structure,
    create_task_structure,
    create_task_graph_template
)

# Create an investigation folder
inv_path = create_investigation_structure(
    root_path=".",
    investigation_name="api-performance-issue",
    description="Investigating slow API response times"
)

# Create a task structure
task_path = create_task_structure(
    root_path=".",
    task_name="implement-caching",
    description="Add Redis caching layer"
)

# Create a task graph template
create_task_graph_template(".")
```

## Development

### Running Tests

```bash
bazel test //llm/ducktape_llm_common/...
```

### Code Quality

```bash
bazel build --config=check //llm/ducktape_llm_common/...
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

## License

MIT License - see LICENSE file for details.

## Support

- Documentation: <https://ducktape.readthedocs.io/>
- Issues: <https://github.com/ducktape/llm-common/issues>
- Discussions: <https://github.com/ducktape/llm-common/discussions>
