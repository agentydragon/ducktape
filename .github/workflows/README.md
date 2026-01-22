# GitHub Workflows

This directory contains GitHub Actions workflows for CI/CD and GitHub Copilot coding agent setup.

## Copilot Setup Steps

**File**: `copilot-setup-steps.yml`

**Purpose**: Configures the environment for GitHub Copilot coding agent before it starts working on tasks.

**What it does**:

1. Checks out the repository code
2. Sets up Python 3.13
3. Installs Bazelisk (for building with Bazel)
4. Caches Bazel artifacts for faster subsequent runs
5. Installs pre-commit hooks
6. Installs cluster tools (opentofu, tflint)

**When it runs**:

- Automatically when the file is changed (for testing)
- Manually via the Actions tab
- Before GitHub Copilot coding agent starts working on an assigned issue

**How to modify**:

1. Edit `copilot-setup-steps.yml`
2. Only modify the `steps`, `permissions`, `runs-on`, `services`, or `timeout-minutes` settings
3. The job MUST be named `copilot-setup-steps`
4. Test by pushing changes or running manually via Actions tab

**Documentation**:

- [Customize the agent environment](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment)
- [Repository docs](.github/docs/COPILOT_CODING_AGENT.md)

## Other Workflows

See individual workflow files for their specific purposes (CI, linting, releases, etc.).
