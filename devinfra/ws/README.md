# `ws` — disposable agent workspaces

> The standalone `agent-workspaces` namespace and Codex warm pool are retired. This
> client is retained for a future compatible pool; it does not manage Agentplane's
> own Sandbox API.

Thin wrapper for Kubernetes `SandboxClaim`s in `agent-workspaces`: claim a pre-warmed
sandbox, shell in, extend, or dispose. The shared upstream controller remains
installed; auth is whatever kubeconfig `kubectl` resolves. No matching namespace or
warm pool is currently deployed.

Use `ws --help` for commands and examples. From the checkout, run
`bb run //devinfra/ws -- --help`.

Shells are persistent: `sh` attaches to (or creates) the pod's `main` tmux
session, so dropped connections and repeated `ws sh` land in the same shell.

Gotcha this tool exists to hide: warm-pool adoption keeps the sandbox's
pool-generated name — the claim's `status.sandbox.name` is the pod handle, not
the claim name.
