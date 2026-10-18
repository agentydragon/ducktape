# `ws` — disposable agent workspaces

> The standalone `agent-workspaces` namespace and Codex warm pool are retired. This
> client is retained for a future compatible pool; it does not manage Agentplane's
> own Sandbox API.

Thin wrapper for Kubernetes `SandboxClaim`s in `agent-workspaces`: claim a pre-warmed
sandbox, shell in, extend, or dispose. The shared upstream controller remains
installed; auth is whatever kubeconfig `kubectl` resolves. No matching namespace or
warm pool is currently deployed.

```bash
ws new -t codex      # claim a warm workspace + shell
ws new -t codex fix --ttl 3d
ws new -t codex exp  # lanes are LLM templates (ws templates lists them)
ws templates       # available lanes + warm-pool readiness
ws ls              # claim → sandbox, pod phase, deadline
ws sh [name]       # shell into a claim (default: newest)
ws extend fix 24h  # push the auto-delete deadline out
ws rm fix          # dispose (claim deletion cascades sandbox + PVC)
ws rm --all
```

Run via Bazel (`bb run //devinfra/ws -- ls`) or alias it:
`alias ws='bazel run --ui_event_filters=-info --noshow_progress //devinfra/ws --'`.

Shells are persistent: `sh` attaches to (or creates) the pod's `main` tmux
session, so dropped connections and repeated `ws sh` land in the same shell.

Gotcha this tool exists to hide: warm-pool adoption keeps the sandbox's
pool-generated name — the claim's `status.sandbox.name` is the pod handle, not
the claim name.
