# GitHub Workflows

## Copilot Setup Steps

[copilot_coding_agent.md](copilot_coding_agent.md) covers `copilot-setup-steps.yml`.

## CodeQL

[codeql.yml](../../.github/workflows/codeql.yml) scans all six configured languages
nightly on the default branch and on manual dispatch. It serializes scans and limits
language jobs to two concurrent runners. PR/push triggers are deliberately absent:
default setup's per-update language fan-out competed with required CI for runners.
Findings arrive after merge, on the next successful scan. Manual dispatch can scan a
selected branch when earlier feedback is useful.

For a missing scan, check the scheduled or manual workflow run and its six analyses
under **Security → Code scanning**. The
`gh api repos/agentydragon/ducktape/code-scanning/default-setup` readback should
report `state: not-configured`. If the generated `dynamic/github-code-scanning/codeql`
workflow starts on PR updates, check the default-setup setting. While enabled, it
blocks the checked-in workflow's analysis uploads. See
[GitHub's advanced-setup instructions](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configuring-advanced-setup-for-code-scanning).
