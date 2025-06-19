# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a multi-language project template. When starting a new project:
1. Delete language-specific sections that don't apply
2. Keep only relevant configuration
3. Update this file to reflect the actual project

## Python-Specific Requirements [DELETE IF NOT PYTHON]

### Virtual Environment (CRITICAL)
**ALWAYS work in a virtual environment. NEVER install packages globally.**

```bash
# Check if in venv
which python  # Should show .../venv/bin/python

# If not in venv, activate it
source venv/bin/activate

# If venv doesn't exist, create it first
python -m venv venv
```

### Test Location Convention
Tests MUST be placed next to the code they test:
- `src/mypackage/foo.py` → `src/mypackage/test_foo.py`
- NOT in `/tests/` directory
- NOT as `test_foo_bar.py` for `foo/bar.py`
- ONLY as `test_*.py` in the same directory as the code

## Development Commands

### Code Quality
```bash
# Run all pre-commit hooks manually
pre-commit run --all-files

# Run specific hooks
pre-commit run black --all-files    # Format Python code
pre-commit run ruff --all-files     # Lint Python code
pre-commit run mypy --all-files     # Type check Python code

# Auto-fix issues where possible
ruff check --fix .
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest path/to/test_file.py

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test function
pytest path/to/test_file.py::test_function_name

# Run tests matching pattern
pytest -k "pattern"

# Run with verbose output
pytest -v
```

### Reference Materials
```bash
# Update reference documentation and examples
cd references && ./fetch.sh
```

## Project Structure

- **`docs/`**: Project documentation
- **`references/`**: External documentation and examples (gitignored except fetch.sh)
  - `fetch.sh`: Updates reference materials from external sources
- **`scratch/`**: Temporary workspace for experiments (gitignored)
  - Agents should create subdirectories like `/scratch/<agent-name>/`
- **`.pre-commit-config.yaml`**: Automated code quality checks

## Code Standards

### Python Development
- **Formatter**: Black (configured in pre-commit)
- **Linter**: Ruff (runs automatically via pre-commit)
- **Type Checker**: mypy (configured for strict checking)
- **Test Framework**: pytest

### Pre-commit Hooks
The following checks run automatically on commit:
- Trailing whitespace removal
- End-of-file fixing
- YAML/JSON/TOML validation
- Python AST validation
- Private key detection
- Large file prevention
- Merge conflict markers detection

### Working with References
The `references/` directory contains fetched external documentation. Only `fetch.sh` is version controlled. To add new references:
1. Edit `references/fetch.sh` to add fetch commands
2. Run the script to fetch materials
3. Commit only the updated `fetch.sh`

### Scratch Directory Usage
For temporary work:
```bash
# Generate agent name at session start
agent_name=$(generate-agent-name)

# Create agent-specific scratch directory
mkdir -p scratch/$agent_name

# Mark files as temporary
echo "# THROWAWAY SCRIPT - DO NOT REUSE" > scratch/$agent_name/experiment.py
```

## Common Confusions for New Instances

1. **Template vs Project**: This starts as a template. Delete irrelevant sections after choosing your language/stack.

2. **Virtual Environment**: For Python, ALWAYS check you're in venv before any pip commands:
   ```bash
   which python  # Must show venv path, not system python
   ```

3. **Test Location**: Tests go NEXT to code, not in separate test directories.

4. **Pre-commit**: Already installed via `pre-commit install`. Runs automatically on commit.

5. **References**: The `references/` dir is gitignored except `fetch.sh`. Add new references by editing `fetch.sh`.

6. **Agent Names**: Use `generate-agent-name` to create a unique identifier for your session's scratch work.
