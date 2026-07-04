# GitHub Workflows

## Copilot Setup Steps

`copilot-setup-steps.yml` configures the environment for GitHub Copilot coding agent. Sets up Python 3.13, Bazelisk, Bazel cache, pre-commit, and cluster tools (opentofu, tflint).

The job MUST be named `copilot-setup-steps`. See [GitHub docs](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment).

## Other Workflows

See individual workflow files for their specific purposes (CI, linting, releases, etc.).
