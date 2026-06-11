# OpenClaw Command Execution & Node Routing

How OpenClaw routes shell command execution, how the node-host sidecar works,
and how to configure multi-node setups.

## Architecture: Where Commands Run

OpenClaw's exec tool has three execution hosts, controlled by `tools.exec.host`:

| `host` value        | Where commands run                     | Our setup               |
| ------------------- | -------------------------------------- | ----------------------- |
| `sandbox` (default) | Isolated container managed by OpenClaw | Not currently used      |
| `gateway`           | The gateway process itself             | Not what we want        |
| `node`              | A paired node-host companion process   | **This is what we use** |

In our deployment, the **node-host sidecar** (see `openclawinstance.yaml`
`sidecars` section) runs as a sidecar container in the same pod as the gateway.
It connects to the gateway over `127.0.0.1:18789` and registers as a node with
`system.run` capability. When the agent calls the exec tool, the gateway routes
the command to this node-host over WebSocket, and the **node-host spawns the
process directly inside its own container** via Node.js `child_process.spawn()`.

No Kubernetes API or service account token is involved in command execution
itself — the sidecar is just a regular Linux process spawning child processes.

```text
┌──────────────────────────────────────────────────────────┐
│ OpenClaw Pod                                             │
│                                                          │
│  ┌──────────────┐    WebSocket     ┌───────────────────┐│
│  │ Gateway      │◄───────────────►│ Node-Host sidecar  ││
│  │ :18789       │  (127.0.0.1)    │                    ││
│  │              │                  │ child_process      ││
│  │ tools.exec   │  routes exec    │ .spawn() ──► bash  ││
│  │ .host=node ──┼────────────────►│           ──► git   ││
│  └──────────────┘                  │           ──► kubectl│
│                                    └───────────────────┘│
└──────────────────────────────────────────────────────────┘
```

## Current Configuration

Our `openclawinstance.yaml` configures the node-host sidecar:

```yaml
sidecars:
  - name: node-host
    image: ghcr.io/openclaw/openclaw:latest
    command: ["node", "openclaw.mjs", "node", "run"]
    args: ["--host", "127.0.0.1", "--port", "18789"]
    env:
      - name: OPENCLAW_GATEWAY_TOKEN
        valueFrom:
          secretKeyRef:
            name: openclaw-gateway-token
            key: token
```

### Sandbox Service Account Token (kubectl only)

The sidecar mounts a service account token from `openclaw-sandbox` namespace at
`/var/run/secrets/kubernetes.io/serviceaccount`. This token is **only relevant
for `kubectl` commands** — it provides in-cluster auth that scopes kubectl to the
`openclaw-sandbox` namespace with restricted RBAC permissions. The token plays no
role in how commands are spawned or routed; it simply determines what kubectl can
access when the agent happens to run `kubectl` inside the sidecar.

## Configuring `tools.exec`

To route exec to the node-host, set these in `config.raw`:

```yaml
config:
  raw:
    tools:
      exec:
        host: node # Route to paired node (not sandbox/gateway)
        security: full # Unrestricted execution
        ask: off # Don't prompt for approval (headless)
        # node: "node-host"  # Optional — auto-selects when only one node connected
```

### `tools.exec.*` Field Reference

| Field         | Type                           | Default                               | Description                                                    |
| ------------- | ------------------------------ | ------------------------------------- | -------------------------------------------------------------- |
| `host`        | `sandbox` / `gateway` / `node` | `sandbox`                             | Where commands execute                                         |
| `node`        | string                         | —                                     | Target node ID or display name (auto-selects if only one node) |
| `security`    | `deny` / `allowlist` / `full`  | `deny`                                | Command validation mode                                        |
| `ask`         | `off` / `on-miss` / `always`   | `on-miss`                             | Approval prompt strategy                                       |
| `askFallback` | `deny` / `allowlist` / `full`  | `deny`                                | Behavior when UI is unreachable                                |
| `timeout`     | number                         | 1800                                  | Kill process after N seconds                                   |
| `yieldMs`     | number                         | 10000                                 | Auto-background after N ms                                     |
| `safeBins`    | list of strings                | `[jq, cut, uniq, head, tail, tr, wc]` | Stdin-only filter utilities allowed without explicit allowlist |
| `pathPrepend` | string/list                    | —                                     | PATH additions (sandbox host only)                             |

### Security Modes

| Mode        | Behavior                                                                          |
| ----------- | --------------------------------------------------------------------------------- |
| `deny`      | Blocks all exec requests                                                          |
| `allowlist` | Only pre-approved binary paths. Shell pipelines require all segments allowlisted. |
| `full`      | Unrestricted execution (equivalent to elevated mode)                              |

Allowlist entries are managed via:

```bash
openclaw approvals allowlist add --node <id> "/usr/bin/git"
openclaw approvals allowlist add --node <id> "/usr/bin/kubectl"
```

Stored in `~/.openclaw/exec-approvals.json` on the node host.

### Per-Agent Exec Binding

Different agents can target different nodes:

```yaml
agents:
  list:
    - name: devbot
      tools:
        exec:
          host: node
          node: "dev-machine"
    - name: opsbot
      tools:
        exec:
          host: node
          node: "prod-bastion"
```

## Multiple Nodes

### Can Multiple Nodes Connect?

**Yes.** Multiple node-host processes can connect to the same gateway
simultaneously. Each node registers with a unique `device.id` (derived from a
keypair fingerprint) and advertises its capabilities during the WebSocket
`connect` handshake.

### Node Registration Flow

1. Gateway sends a **pre-connect challenge** with a nonce and timestamp
2. Node responds with `connect` request declaring `role: "node"` and a stable
   `device.id` (keypair fingerprint), signing the challenge nonce
3. Node advertises capabilities via three fields:
   - `caps` — high-level categories (`system.run`, `camera`, `canvas`, etc.)
   - `commands` — allowlist of invocable commands (`camera.snap`, `screen.record`)
   - `permissions` — granular toggles reflecting device permission status
4. Gateway checks `OPENCLAW_GATEWAY_TOKEN` authentication
5. If the device ID is new, gateway requires **pairing approval** (unless
   `dangerouslyDisableDeviceAuth: true` or the connection is from loopback)
6. Node appears in `openclaw nodes status`

Supported node types that can coexist: macOS nodes, iOS/Android nodes, and
headless Linux node-hosts.

### Node Management Commands

```bash
# Listing
openclaw nodes list                            # All pending and paired nodes
openclaw nodes list --connected                # Only currently connected
openclaw nodes list --last-connected 24h       # Filter by recency
openclaw nodes pending                         # Pending pairing requests
openclaw nodes status                          # Node status overview

# Approval
openclaw devices list                          # All known devices
openclaw devices approve <requestId>           # Approve a new node

# Direct invocation from CLI
openclaw nodes invoke --node <id> --command <cmd> --params '{}'
openclaw nodes run --node <id> "git status"    # Run shell command on node
```

### Can the Agent Choose Which Node to Use?

**Yes.** Node selection works at four levels, from static config down to
per-invocation:

| Level                | Config                                                                  | Scope                    |
| -------------------- | ----------------------------------------------------------------------- | ------------------------ |
| Global default       | `tools.exec.node: "node-name"`                                          | All agents, all sessions |
| Per-agent binding    | `agents.list[N].tools.exec.node: "node-name"`                           | Specific agent           |
| Session override     | `/exec node=mac-1` (operator slash command)                             | Current session only     |
| Per-invocation (LLM) | `{"command": "...", "host": "node", "node": "mac-1"}` in exec tool call | Single tool call         |

The LLM can pass `host` and `node` parameters directly in the exec tool
invocation, allowing dynamic node selection per command. This means you can
expose multiple nodes and let the agent decide which one to target based on the
task at hand.

**Practical approaches for multi-node agent routing:**

1. **Per-invocation selection** — the agent passes `node: "<name>"` in each
   exec tool call. Requires the system prompt to describe available nodes and
   when to use each one.
2. **Per-agent binding** — different agents target different nodes, using a
   coordinator/sub-agent pattern for delegation.
3. **Custom MCP tool** — wrap node selection logic in a dedicated tool.

### Multi-Node Sidecar Example

To add a second node (e.g., a GPU compute node), add another sidecar:

```yaml
sidecars:
  - name: node-host
    image: ghcr.io/openclaw/openclaw:latest
    command: ["node", "openclaw.mjs", "node", "run"]
    args: ["--host", "127.0.0.1", "--port", "18789", "--display-name", "sandbox-node"]
    env:
      - name: OPENCLAW_GATEWAY_TOKEN
        valueFrom: { secretKeyRef: { name: openclaw-gateway-token, key: token } }

  - name: gpu-node
    image: ghcr.io/openclaw/openclaw:latest
    command: ["node", "openclaw.mjs", "node", "run"]
    args: ["--host", "127.0.0.1", "--port", "18789", "--display-name", "gpu-node"]
    env:
      - name: OPENCLAW_GATEWAY_TOKEN
        valueFrom: { secretKeyRef: { name: openclaw-gateway-token, key: token } }
```

Then bind agents to specific nodes:

```yaml
agents:
  list:
    - name: main
      tools:
        exec:
          host: node
          node: "sandbox-node"
    - name: compute
      tools:
        exec:
          host: node
          node: "gpu-node"
```

### Remote Nodes (Outside the Pod)

Nodes don't have to be sidecars. Any machine can connect:

```bash
# On a remote machine
openclaw node run --host <gateway-host> --port 18789 --display-name "my-workstation"
```

If the gateway binds to localhost only, use SSH tunneling:

```bash
ssh -N -L 18790:127.0.0.1:18789 user@gateway-host
openclaw node run --host 127.0.0.1 --port 18790 --display-name "remote-node"
```

## Node Capabilities

Nodes advertise capabilities at connect time. Available capability families:

| Capability        | Commands                                            | Platform                     |
| ----------------- | --------------------------------------------------- | ---------------------------- |
| System execution  | `system.run`, `system.which`, `system.notify`       | All (headless included)      |
| Canvas operations | `canvas.snapshot`, `canvas.eval`, `canvas.navigate` | macOS/iOS/Android            |
| Camera access     | `camera.list`, `camera.snap`, `camera.clip`         | macOS/iOS/Android            |
| Screen recording  | `screen.record`                                     | macOS/iOS/Android            |
| Location services | `location.get`                                      | iOS/Android (off by default) |
| SMS               | `sms.send`                                          | Android only                 |
| Browser proxy     | (automatic)                                         | All (if not disabled)        |

**Headless Linux node-hosts** (like our sidecar) naturally only expose
`system.run`/`system.which` and browser proxy — camera, canvas, screen, and
location capabilities require hardware/OS features not present in containers.

There is no `--capabilities` CLI flag to selectively enable/disable capability
families. Instead, restrict the effective surface through:

1. **Platform limitation** — headless Linux inherently limits to exec + browser
2. **Gateway tool policies** — `agents.defaults.tools.allow`/`deny` lists:
   ```yaml
   agents:
     defaults:
       tools:
         allow: ["exec", "read"]
         deny: ["browser", "canvas", "nodes", "cron"]
   ```
3. **Exec allowlists** — restrict which binaries the node can run
4. **`browser.enabled: false`** — disable browser proxy capability

## Approval Flow

When `tools.exec.security` is `allowlist` and `ask` is `on-miss`:

1. Agent requests command execution
2. Gateway checks allowlist for the resolved binary path
3. If not allowlisted, gateway broadcasts `exec.approval.requested`
4. Operator (via CLI, web UI, or macOS app) sees approval dialog with:
   command, working directory, agent ID, resolved path, host
5. Operator chooses: **Allow once**, **Always allow** (adds to allowlist), or **Deny**
6. Gateway forwards approved command to node-host

For headless/automated setups, set `ask: off` and either pre-populate the
allowlist or use `security: full`.

## Node-Host CLI Reference

The node-host is a subcommand of the main `openclaw` CLI (requires Node.js

> = 22):

```bash
openclaw node run --host <gateway-host> --port 18789 --display-name "My Node"
```

| Flag                      | Default     | Description                               |
| ------------------------- | ----------- | ----------------------------------------- |
| `--host`                  | `127.0.0.1` | Gateway WebSocket host                    |
| `--port`                  | `18789`     | Gateway WebSocket port                    |
| `--tls`                   | off         | Enable TLS (`wss://`)                     |
| `--tls-fingerprint <sha>` | —           | Validate server certificate fingerprint   |
| `--node-id <id>`          | auto        | Override node identifier (clears pairing) |
| `--display-name <name>`   | auto        | Custom node display name                  |
| `--token <token>`         | —           | Gateway auth token (or use env var)       |

Service management (for persistent background nodes):

```bash
openclaw node install    # Install as background service
openclaw node status     # Check service status
openclaw node stop       # Stop background node
openclaw node restart    # Restart background node
openclaw node uninstall  # Remove background service
```

Node identity and connection details persist in `~/.openclaw/node.json`.

## Security Constraints on Nodes

- Host execution (`gateway`/`node`) rejects `LD_*`/`DYLD_*` loader overrides
- `env.PATH` overrides are blocked to prevent binary hijacking
- Nodes receive only non-blocked environment variables
- Exec approvals enforced locally at `~/.openclaw/exec-approvals.json`
- When `autoAllowSkills` is enabled in approvals config, executables referenced
  by known skills are pre-allowlisted on nodes (via `skills.bins` gateway RPC)

## References

- [OpenClaw Exec Tool](https://docs.openclaw.ai/tools/exec)
- [OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals)
- [OpenClaw Gateway Configuration](https://docs.openclaw.ai/gateway/configuration)
- [OpenClaw Gateway Protocol](https://docs.openclaw.ai/gateway/protocol)
- [OpenClaw Nodes](https://docs.openclaw.ai/platforms/nodes)
- [OpenClaw Nodes CLI](https://docs.openclaw.ai/cli/nodes)
- [OpenClaw Node Troubleshooting](https://docs.openclaw.ai/platforms/nodes/troubleshooting)
- [OpenClaw Multi-Agent Routing](https://docs.openclaw.ai/concepts/multi-agent)
- [OpenClaw Gateway Security](https://docs.openclaw.ai/gateway/security)
- [OpenClaw Node CLI Reference](https://docs.openclaw.ai/cli/node)
- [OpenClaw Remote Gateway Access](https://docs.openclaw.ai/gateway/remote)
- [OpenClaw K8s Operator](https://github.com/OpenClaw-rocks/k8s-operator)
- Our instance config: <../k8s/agents/openclaw/gateway/openclawinstance.yaml>
- Sandbox RBAC: <../k8s/agents/openclaw/sandbox/role-sandbox.yaml>
