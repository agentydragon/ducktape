# OpenClaw Execution Model and Security Boundaries

**Date**: 2026-02-24

## Three Exec Host Modes

OpenClaw has three `tools.exec.host` modes for running agent shell commands:

| Mode      | Where commands run                     | Default security | Intended use                                |
| --------- | -------------------------------------- | ---------------- | ------------------------------------------- |
| `sandbox` | Docker container on gateway host       | `deny`           | Default. Local isolation for agent commands |
| `gateway` | Gateway process directly               | `allowlist`      | Dev/legacy. No isolation                    |
| `node`    | Remote paired device via WebSocket RPC | `allowlist`      | Companion apps (macOS, phone)               |

Source: `src/agents/bash-tools.exec.ts` — default host is `sandbox` (line 372).

## Sandbox Mode (Default, Intended)

Documented at `docs/gateway/sandboxing.md`: **"The Gateway stays on the host; tool execution
runs in an isolated sandbox when enabled."**

The gateway creates Docker containers locally. Configuration options:

- **Workspace mount**: `none` (isolated), `ro` (read-only), `rw` (read-write)
- **Network**: disabled by default (`network: "none"`)
- **Scope**: `session` (per session), `agent` (per agent), `shared` (all sessions)
- **Security**: seccomp/apparmor profiles, memory/cpu limits, `readOnlyRoot`, `capDrop`
- **Custom mounts**: `binds: ["host:container:mode"]`

Built-in file tools (Read/Write/Glob) route through the sandbox when active — workspace
files are mounted according to the configured access mode. This avoids the split-brain
filesystem problem.

## Node Mode (What We're Using)

Designed for **companion devices** (macOS app, phone, remote machine). A node-host process
connects to the gateway via WebSocket and registers commands it supports (`system.run`,
`system.which`, `browser.proxy`). The gateway forwards exec requests as `node.invoke` RPC.

Nodes are dumb shell executors — they receive a command, `spawn()` it, return
stdout/stderr/exit code.

## Our Architecture: Node-Host Sidecar with Shared Workspace

**Decision**: Use `host=node` with a sidecar container that shares the workspace PVC with
the gateway. Separate `openclaw-sandbox` namespace for sidecar RBAC isolation.

### How It Works

The gateway and node-host sidecar run in the same pod (`openclaw-0`). The operator-managed
`data` PVC is mounted into both containers at `/home/openclaw/.openclaw`, so built-in file
tools (Read/Write/Glob on the gateway) and shell commands (`child_process.spawn()` on the
sidecar) see the same files. This fixes the split-brain filesystem problem.

The sidecar connects to the gateway via `127.0.0.1:18789` (same pod network namespace),
which is recognized as a local client for device pairing auto-approval.

### Security Model

**Gateway container** (holds secrets):

- MCP server API keys, LLM provider keys injected via `spec.env`
- Operator sets `AutomountServiceAccountToken: false` — no k8s credentials
- `extraVolumes`/`extraVolumeMounts` removed — gateway has zero k8s API access

**Sidecar container** (agent shell execution):

- Sandbox SA token mounted at `/var/run/secrets/kubernetes.io/serviceaccount`
- RBAC scoped to `openclaw-sandbox` namespace (restricted)
- No access to gateway env vars (container isolation)

**Secret flow for SA token**: `openclaw-sandbox/sandbox-sa-token` (long-lived SA token)
→ ClusterSecretStore (`kubernetes-openclaw-sandbox-secret-store`) → ExternalSecret in
`openclaw` namespace → sidecar volume mount.

### MCP Server Security

The gateway process hosts MCP servers. The agent interacts with them through airlock-gated
gateway tools. Two transport types with different security properties:

- **Stdio MCP servers**: Safe by default. Pipe-based IPC, child process of gateway. The
  sidecar cannot reach them — there's no network path to a Unix pipe.
- **HTTP MCP servers**: Share `localhost` (same pod network namespace). The sidecar _can_
  reach them on localhost, bypassing gateway approval gating. **Mitigation**: HTTP MCP
  servers must require bearer token auth. The gateway has the token (injected via env),
  the sidecar doesn't → 401 Unauthorized.

### Why Not Gateway-Side Execution

`host=gateway` would let the agent extract MCP server API tokens from gateway env vars via
`env | grep TOKEN` or `cat /proc/1/environ`. The sidecar approach keeps secrets in the
gateway process while letting the agent use them through airlock-gated tools.

### Why Not Sandbox Mode

Sandbox mode (`host=sandbox`) requires Docker-in-Docker — the gateway would need to create
Docker containers. DinD requires privileged mode (blocked by Kyverno), Sysbox is unavailable
on Talos, and rootless DinD is impractical. The operator's webhook rejects privileged
containers.

## Agent Definition Files

Agent definitions are **gateway-side only**, loaded at session start without node
involvement:

| File           | Purpose                                |
| -------------- | -------------------------------------- |
| `AGENTS.md`    | Core system prompt / instructions      |
| `SOUL.md`      | Personality, behavioral guidelines     |
| `TOOLS.md`     | Tool usage instructions                |
| `IDENTITY.md`  | Name, emoji, creature, vibe            |
| `USER.md`      | User preferences and context           |
| `MEMORY.md`    | Persistent memory (agent reads/writes) |
| `HEARTBEAT.md` | Cron/heartbeat task instructions       |
| `BOOTSTRAP.md` | First-run onboarding                   |

These live in the workspace directory on the gateway's PVC. The gateway reads them via
`loadWorkspaceBootstrapFiles()` and injects them into the LLM system prompt. Nodes never
see these files.

## K8s Operator Capabilities

The OpenClaw k8s operator (`/code/github.com/openclaw-rocks/k8s-operator`) is hardened by
default but has **no native Docker-in-Docker or sandbox container creation support**:

- **Pod security**: non-root (UID 1000), read-only rootfs, all capabilities dropped,
  seccomp RuntimeDefault, `allowPrivilegeEscalation: false`
- **Network policy**: default-deny ingress, explicit allowlist (DNS, HTTPS egress)
- **RBAC**: dedicated SA per instance, least-privilege (only `get`/`watch` own ConfigMap)
- **Native sidecars**: Chromium (browser automation) and Ollama (LLM inference) built-in
- **Custom sidecars**: `spec.sidecars` accepts arbitrary `corev1.Container` specs
- **Custom volumes**: `spec.sidecarVolumes`, `spec.extraVolumes`, `spec.extraVolumeMounts`
- **Init pipeline**: ordered init containers (config → pnpm → python → skills → ollama → custom)
- **Gateway token**: auto-generated Secret, injected as `OPENCLAW_GATEWAY_TOKEN`
- **Environment injection**: `spec.env`, `spec.envFrom` (Secrets/ConfigMaps), plus
  auto-injected vars (`HOME`, `CHROMIUM_URL`, `OLLAMA_HOST`, etc.)

The operator does **not** support:

- Docker-in-Docker (would require privileged mode, which the webhook rejects)
- gVisor/Kata container runtimes
- Application-level sandbox container creation

The `sandbox` field in OpenClaw config (`agents.defaults.sandbox`) is **application-level**
sandboxing (Docker containers created by the gateway process), not k8s-level. It cannot
work without Docker access from the gateway container.

## Key Takeaway

The intended OpenClaw security model is `host=sandbox` with Docker isolation (not available
in k8s without DinD). Our sidecar-with-shared-workspace approach provides: filesystem
consistency (shared PVC), secret isolation (gateway env vars unreachable from sidecar),
and RBAC scoping (sandbox SA token). MCP servers are protected by transport type — stdio
is inherently safe, HTTP must use token auth.
