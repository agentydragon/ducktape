# Ducktape setup for Codex Cloud (Legacy)

Use these commands in a legacy environment for Code Review or the GitHub and
Linear integrations. The current web, mobile and desktop environments have a
separate setup workflow. [Official legacy guide](https://learn.chatgpt.com/docs/environments/cloud-environment)
and [current environment guide](https://learn.chatgpt.com/docs/environments/cloud-environments).

| Environment setting | Command                                                 |
| ------------------- | ------------------------------------------------------- |
| Setup script        | `bash devinfra/codex_cloud/setup.sh --mode=install`     |
| Maintenance script  | `bash devinfra/codex_cloud/setup.sh --mode=maintenance` |

## What this setup does

On a fresh Linux x86_64 image, [setup.sh](setup.sh) installs Determinate Nix if
needed. On cache resume, maintenance loads the Nix profile and fetches Git
remotes. Both paths refresh `.#devtools`, install pre-commit hooks and configure
the agent shell. Maintenance also checks that `bb` and `bbr` start.

- [BuildBuddy setup](../buildbuddy/setup.sh) writes its credential when
  `BUILDBUDDY_API_KEY` is available. [Remote reconciliation](../claude/reconcile_bbr_remote.sh)
  selects GitHub directly or configures `github-no-proxy`, and sets up tracking
  for the remote default branch so `bbr` can find its base.
- Under `~/.config/bazel/`, `bbr.bazelrc` sets the role and session tag.
  `codex.bazelrc` imports that metadata and the BuildBuddy credential, selects
  the agent config, and filters live OpenAI tests. The setup adds its import to
  `~/.bazelrc`.
- With a SOPS key, [web_env.sh](../secrets/web_env.sh) supplies tool credentials
  and the Nix profile's Python runs [kubeconfig.py](../k8s/kubeconfig.py) to
  materialize `~/.kube/config`.

## Agent shell and credentials

**Supply `SOPS_AGE_KEY` only when agent tasks may use it.** Legacy Cloud
removes setup-only secrets from the process environment before the agent runs.
Ducktape persists any supplied key and resolved BuildBuddy key in the mode-0600
`~/.codex/agent_env.sh`. Supplying the age key through the setup-only secret
field therefore still grants runtime access. A setup-only decryption requirement
needs a change to that persistence behavior. [Official secret lifecycle](https://learn.chatgpt.com/docs/environments/cloud-environment#environment-variables-and-secrets).

The generated file selects the Nix tools, sources `web_env.sh` when the key is
available, and exports `BBR_BAZELRC` and `SESSION_BAZELRC`. Setup arranges for
the Bash login and interactive startup files to source it. For a fresh
non-interactive Bash command that skips those files, load it in that command:

```bash
source "$HOME/.codex/agent_env.sh"
bbr test //devinfra/bazel:test_workspace
```

If cached state is incompatible with the current checkout, use **Reset cache**
on the legacy environment page. Setup and maintenance configuration changes
also invalidate the cache. [Cache behavior](https://learn.chatgpt.com/docs/environments/cloud-environment#container-caching).
