# `ws` — disposable agent workspaces

CLI for the agent workspaces in <../cluster/k8s/agents/agent-sandbox/>: claim a
pre-warmed sandbox, shell in, extend, dispose. Thin wrapper over `kubectl`
against `SandboxClaim`s in `agent-workspaces` — auth is whatever kubeconfig
`kubectl` resolves (the namespace is operator-only).

```bash
ws new             # claim a warm workspace (ready in seconds) + shell in
ws new fix --ttl 3d
ws ls              # claim → sandbox, pod phase, deadline
ws sh [name]       # shell into a claim (default: newest)
ws extend fix 24h  # push the auto-delete deadline out
ws rm fix          # dispose (claim deletion cascades sandbox + PVC)
ws rm --all
```

Run via Bazel (`bb run //ws -- ls`) or alias it:
`alias ws='bazel run --ui_event_filters=-info --noshow_progress //ws --'`.

Shells are persistent: `sh` attaches to (or creates) the pod's `main` tmux
session, so dropped connections and repeated `ws sh` land in the same shell.

Gotcha this tool exists to hide: warm-pool adoption keeps the sandbox's
pool-generated name — the claim's `status.sandbox.name` is the pod handle, not
the claim name. `ws` resolves it for you.
