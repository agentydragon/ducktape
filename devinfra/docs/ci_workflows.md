# GitHub Workflows

## Copilot Setup Steps

`copilot-setup-steps.yml` configures the environment for GitHub Copilot coding agent. Sets up Python 3.13, Bazelisk, Bazel cache, pre-commit, and cluster tools (opentofu, tflint).

The job MUST be named `copilot-setup-steps`. See [GitHub docs](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment).

## Other Workflows

See individual workflow files for their specific purposes (CI, linting, releases, etc.).

## CodeQL

[codeql.yml](../../.github/workflows/codeql.yml) scans all six configured languages
nightly on the default branch and on manual dispatch. It serializes scans and limits
language jobs to two concurrent runners. PR/push triggers are deliberately absent:
default setup's per-update language fan-out competed with required CI for runners.
Findings arrive after merge, on the next successful scan. Manual dispatch can scan a
selected branch when earlier feedback is useful.

### Switching from default setup

After merging the workflow into `devel`:

1. Open repository **Settings → Advanced Security**, then the menu beside
   **CodeQL analysis → Switch to advanced**.
2. Confirm **Disable CodeQL**. This disables the generated default configuration;
   the checked-in workflow owns scanning. If GitHub opens a starter-workflow editor,
   dismiss it: `codeql.yml` already supplies the replacement.
3. Open **Actions → CodeQL scheduled → Run workflow**, select `devel`, and run it.
4. Verify all six language jobs succeed and their analyses appear under
   **Security → Code scanning** for `devel`. Verify a subsequent PR update does not
   start the generated `dynamic/github-code-scanning/codeql` workflow.

Default setup blocks advanced-setup analysis uploads while enabled; merging the YAML
alone does not switch modes. The API readback
`gh api repos/agentydragon/ducktape/code-scanning/default-setup` should report
`state: not-configured` after the switch. Already queued/running generated scans may
need separate cancellation to immediately reclaim their slots.

[GitHub's migration instructions](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configuring-advanced-setup-for-code-scanning).
