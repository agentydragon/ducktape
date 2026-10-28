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

## 4) Practical configuration plan for Codex Cloud (Legacy) in this repo

Goal: make Codex Cloud behave as close as feasible to our canonical Nix/devshell workflows, while remaining robust when hooks are unavailable.

## 4.1 Baseline environment shape (recommended)

1. **Codex Cloud environment image**: use OpenAI universal image.
2. **Setup script**: perform deterministic bootstrap for this repo:
   - install/enable required tooling available in Cloud context
   - run BuildBuddy bootstrap (`devinfra/setup_buildbuddy.sh`) when key present
   - run quick sanity checks (`bb --version`, `bbr --help`, `python --version`, etc.)
3. **Maintenance script**: lightweight idempotent refresh for cached containers:
   - `git fetch --all --prune`
   - validity checks for bb auth / remote availability
4. **Environment variables** (non-secret):
   - repo-level defaults (`CI=1`, optionally `CODEX_AGENT=1`)
   - any stable non-sensitive flags
5. **Secrets / decryption inputs**:
   - no direct `BUILDBUDDY_API_KEY` environment injection required
   - `SOPS_AGE_KEY` available so setup/maintenance can decrypt
     `cluster/k8s/external-creds/buildbuddy-api-key.sops.yaml`
   - any additional tokens needed only to install/private-fetch during setup
6. **Internet policy**:
   - start with allowlist mode, minimum domains required for setup/package fetch + repo infra endpoints.

## 4.2 Agent behavior controls

- Keep authoritative repository guidance in `AGENTS.md`.
- Maintain deterministic command recipes in AGENTS over implicit hook behavior.

Reasoning: AGENTS.md behavior in Cloud is documented; hooks behavior in Cloud is not.

## 4.3 Hooks strategy (pending verification)

Because Cloud hook execution is not explicitly documented:

- Do **not** make Cloud correctness depend on `.codex/hooks.json`.
- If desired, add an experiment suite:
  1. commit a minimal project `.codex/hooks.json` with observable side effects,
  2. run a no-op Cloud task,
  3. inspect filesystem/log artifacts for hook execution,
  4. repeat across fresh + cached container runs.
- If hooks execute reliably, treat as optional acceleration only; keep setup+AGENTS as source of truth.

## 4.4 Nix/devshell and SOPS in Cloud: practical path

"Ideal" parity with local Nix devshell is desirable, but in managed Cloud containers this should be staged:

### Stage A (now): tool parity without full Nix activation

- Install/use repo tooling directly in setup script (bb/bbr/python/pre-commit/etc.) sufficient for agent tasks.
- Use Bazel + RBE as the execution substrate.
- Keep bootstrap fast to preserve container cache value.

### Stage B (optional): partial Nix enablement

- If Cloud image/runtime permits, install Nix in setup and pre-build needed profile/packages.
- Cache warm-up costs may be significant; verify payoff against 12h cache behavior.

### Stage C (advanced): canonical devshell parity

- Attempt `nix develop` style parity only if setup-time and reliability are acceptable.
- If this path becomes canonical, codify strict smoke tests in setup and maintenance scripts.

### SOPS specifics

- Provide `SOPS_AGE_KEY` via environment configuration so setup/maintenance and
  agent tasks can decrypt as needed.
- Use SOPS decryption of encrypted files under repo paths (for example the
  BuildBuddy key) rather than duplicating raw secret values into environment
  config fields.
- Ensure decryption paths respect repository-relative creation rules.

## 5) Reachable knobs checklist (Cloud-first)

Use this as the operator checklist for Codex Cloud environment config:

- [ ] Setup script configured and idempotent
- [ ] Maintenance script configured and idempotent
- [ ] Required non-secret env vars declared
- [ ] SOPS decryption inputs declared (for encrypted repo-managed secrets)
- [ ] Internet policy set to least privilege needed
- [ ] Cache reset procedure documented for bad state
- [ ] Hook support experimentally verified (optional; do not block rollout)

## 6) Open questions / follow-up research

1. Does Codex Cloud (Legacy) execute project `.codex/hooks.json` in delegated tasks?
2. If yes, are all hook events supported in Cloud, or only a subset?
3. Are hook side effects persisted in cached container resumes exactly once or per resumed session?
4. What are current setup script timeout and resource ceilings by plan tier?

Until (1)-(2) are verified, this repo should treat hooks as non-authoritative
for legacy Cloud tasks. Current Cloud orchestration's local-hook restriction is
already documented above and does not answer question (1).
