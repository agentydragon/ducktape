# Codex Cloud (Legacy): environment behavior, hookability, and ducktape configuration plan

_Last checked: 2026-10-28_

This plan and `setup.sh` describe the Codex Cloud (Legacy) environment flow,
which remains available for Code Review and the GitHub and Linear integrations.
The current Cloud experience for web, mobile, and desktop has separate
environment documentation and setup controls; do not infer its behavior from
the legacy workflow.

## 1) What is known (documented) about Codex Cloud environments

Primary source: OpenAI's Codex Cloud (Legacy) environment docs.

- Codex Cloud runs tasks in an isolated container, checks out the requested branch/SHA, runs setup, then runs the agent loop.
- Setup scripts run in a different shell session than the agent loop; shell exports from setup do not automatically carry into agent shell unless persisted (for example via shell startup files).
- Environment variables configured in the Codex environment are available for the whole task (setup + agent).
- Secrets are available during setup, then removed before the agent phase.
- Setup has internet by default; agent internet is controlled by environment internet policy (off by default with explicit allow/deny options).
- Container caching keeps setup state for up to 12 hours. Follow-up tasks may resume cached state, and cache is invalidated by setup/maintenance script changes and env/secret changes.

References:

- https://learn.chatgpt.com/docs/environments/cloud-environment
- https://learn.chatgpt.com/docs/cloud/internet-access

## 2) Does Codex Cloud (Legacy) follow `.codex` settings and hooks?

## 2.1 What OpenAI docs clearly say

- `.codex/config.toml` and `hooks.json` are documented as Codex config surfaces (config layers include user and project `.codex` paths).
- Hook docs define events, matcher semantics, and configuration format.
- AGENTS.md is explicitly documented and is used for repository-specific instructions.

References:

- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/hooks
- https://learn.chatgpt.com/docs/agent-configuration/agents-md

## 2.2 What is _not_ clearly documented for Cloud

I could not find explicit OpenAI documentation stating that legacy Cloud
environment tasks execute project/user `hooks.json` from `.codex` inside the
Cloud container. The current Cloud hooks guide now says that local/project
hooks and command/shell hooks are unsupported with cloud orchestration. That
describes the current Cloud workflow and does not establish legacy behavior.

The legacy environment docs cover setup and maintenance scripts, `AGENTS.md`,
and internet policy, but do not mention hook execution in the legacy runtime.

### Conclusion (confidence: medium)

- **Documented for Codex Cloud (Legacy)**: setup and maintenance scripts,
  environment variables, setup-only secrets, `AGENTS.md`, and internet policy.
- **Not explicitly confirmed for legacy delegated tasks**: project
  `.codex/hooks.json` execution.
- **Documented for current Cloud orchestration**: local/project hooks and
  command/shell handlers are unsupported. Keep this distinct from legacy
  environment tasks.
- Treat hooks as non-authoritative for this legacy setup until its behavior is
  validated experimentally.

## 2.3 What is hookable where hooks are supported

From hooks docs (general Codex hooks surface):

- Lifecycle: `SessionStart`, `Stop`
- Prompt/input: `UserPromptSubmit`
- Tool interception: `PreToolUse`, `PostToolUse`
- Matchers: specific tool names, shell matchers, and event-specific filters

Reference:

- https://learn.chatgpt.com/docs/hooks

## 2.4 Additional online signals checked

- OpenAI Codex open-source issue traffic around hooks is predominantly tagged/phrased as **CLI** behavior and feature requests (event coverage, matcher semantics, platform parity), not Cloud runtime guarantees.
- I did not find an OpenAI source that positively states: \"Cloud delegated tasks execute `.codex/hooks.json`\".
- Therefore, for Cloud planning in this repo, hooks should be considered experimental until empirically validated in our own environment.

References:

- https://github.com/openai/codex/issues?q=is%3Aissue+hooks+is%3Aopen
- https://github.com/openai/codex/issues/16226
- https://github.com/openai/codex/issues/16301

## 3) Repo-specific constraints relevant to Cloud setup

From this repository's AGENTS/README and current infra:

- Build/test convention is Bazel via `bbr` wrapper for remote execution.
- Session setup breakage (cert/proxy/buildbuddy bootstrap) should be treated as a hard failure and recovered before proceeding.
- SOPS behavior depends on running from repo context and having `SOPS_AGE_KEY` available.
- Canonical local developer environment is Nix-based (`flake.nix`, home-manager, codex module under `nix/home/codex`).
- Existing repo script `devinfra/setup_buildbuddy.sh` configures BuildBuddy based on `BUILDBUDDY_API_KEY`.

References:

- `AGENTS.md`
- `README.md`
- `flake.nix`
- `nix/home/codex/default.nix`
- `devinfra/setup_buildbuddy.sh`

## 4) Remaining rollout work

`setup.sh` now implements the bootstrap and maintenance actions that were
previously proposed here: Nix and the `.#devtools` profile, BuildBuddy setup,
agent shell configuration, and cached-environment refresh. The [README](README.md)
is the source of truth for those behaviors and the operator commands. This plan
tracks only configuration and verification that still require a real Cloud
environment.

### Verify the installed tool profile

- [ ] Run the setup and maintenance modes in fresh and resumed legacy Cloud
      environments. Confirm the agent shell loads the generated environment and
      BuildBuddy-backed `bbr` commands can reach the configured service.
- [ ] Confirm whether the installed `.#devtools` profile provides the tool and
      environment parity this repository needs. The script does not run `nix
develop`; test that path only if the profile leaves a concrete gap, and
      measure setup time before making it the default.
- [ ] Measure fresh setup time and cache-resume behavior against the current
      environment limits; document any recovery steps needed after a failed setup.

### Complete environment configuration

- [ ] Configure the legacy environment with the setup and maintenance commands
      documented in the README.
- [ ] Provide `SOPS_AGE_KEY` as a runtime environment variable only when agent
      tasks may access it. The script currently persists any supplied key into
      the agent environment file, even when it arrived as a setup-only secret.
      If setup needs the key without runtime access, change and verify that
      persistence behavior before configuring the secret.
- [ ] Decide whether tasks need repo-level environment defaults such as
      `CI=1` or `CODEX_AGENT=1`; configure only values required by actual tools.
- [ ] Restrict outbound internet access to the domains required by the setup
      script and repository workflows, then verify the configured BuildBuddy and
      package services from a real task.
- [ ] Record the environment cache reset procedure for a failed or incompatible
      setup state.
- [ ] Keep hook support optional; do not make repo correctness depend on it.

## 5) Open questions / follow-up research

1. Does Codex Cloud (Legacy) execute project `.codex/hooks.json` in delegated tasks?
2. If yes, are all hook events supported in Cloud, or only a subset?
3. Are hook side effects persisted in cached container resumes exactly once or per resumed session?
4. What are current setup script timeout and resource ceilings by plan tier?

To test hook behavior, use a minimal project hook with an observable side
effect, run no-op tasks in fresh and resumed legacy environments, and inspect
the resulting files or logs. Keep hooks optional even if they run reliably.

Until (1)-(2) are verified, this repo should treat hooks as non-authoritative
for legacy Cloud tasks. Current Cloud orchestration's local-hook restriction is
already documented above and does not answer question (1).
