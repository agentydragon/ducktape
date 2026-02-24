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

In our deployment, the **node-host sidecar** (lines 274-305 of
`openclawinstance.yaml`) runs as a sidecar container in the same pod as the
gateway. It connects to the gateway over `127.0.0.1:18789` and registers as a
node with `system.run` capability. When the agent calls the exec tool, the
gateway routes the command to this node-host, which spawns the process.

```text
┌─────────────────────────────────────────────────┐
│ OpenClaw Pod                                    │
│                                                 │
│  ┌──────────────┐    WebSocket     ┌──────────┐│
│  │ Gateway      │◄───────────────►│ Node-Host ││
│  │ :18789       │  (127.0.0.1)    │ sidecar   ││
│  │              │                  │           ││
│  │ tools.exec   │  routes exec    │ spawns    ││
│  │ .host=node ──┼─────────────────►│ processes ││
│  └──────────────┘                  └──────────┘│
└─────────────────────────────────────────────────┘
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

The sidecar uses the sandbox service account token to scope `kubectl` commands
to the `openclaw-sandbox` namespace with restricted RBAC permissions.

## Configuring `tools.exec`

To route exec to the node-host, set these in `config.raw`:

```yaml
config:
  raw:
    tools:
      exec:
        host: node # Route to paired node (not sandbox/gateway)
        node: "node-host" # Name/ID of the target node
        security: allowlist # Or "full" for unrestricted
        ask: off # Don't prompt for approval (headless)
```

### `tools.exec.*` Field Reference

| Field         | Type                           | Default                               | Description                                                    |
| ------------- | ------------------------------ | ------------------------------------- | -------------------------------------------------------------- |
| `host`        | `sandbox` / `gateway` / `node` | `sandbox`                             | Where commands execute                                         |
| `node`        | string                         | —                                     | Target node ID or display name (required when `host=node`)     |
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

1. Node connects to gateway WebSocket, declares `role: node`
2. Node advertises capabilities: `system.run`, `camera.snap`, `canvas.navigate`, etc.
3. Gateway checks `OPENCLAW_GATEWAY_TOKEN` authentication
4. If the device ID is new, gateway requires **pairing approval** (unless
   `dangerouslyDisableDeviceAuth: true`)
5. Node appears in `openclaw nodes status`

### Node Management Commands

```bash
openclaw nodes status                          # List all connected nodes
openclaw nodes describe --node <idOrNameOrIp>  # Node details + capabilities
openclaw devices list                          # List all known devices
openclaw devices approve <requestId>           # Approve a new node
```

### Can the Agent Choose Which Node to Use?

**Not automatically.** The gateway routes exec to whichever node is configured
in `tools.exec.node`. The AI agent itself cannot dynamically select between
nodes — routing is determined by config, not by the LLM.

There are three levels of node selection:

| Level             | Config                                        | Scope                    |
| ----------------- | --------------------------------------------- | ------------------------ |
| Global default    | `tools.exec.node: "node-name"`                | All agents, all sessions |
| Per-agent binding | `agents.list[N].tools.exec.node: "node-name"` | Specific agent           |
| Session override  | `/exec node=mac-1` (operator slash command)   | Current session only     |

**If you want agent-driven node selection**, you would need to:

1. Run multiple agents, each bound to a different node
2. Use a coordinator/sub-agent pattern where a parent agent delegates to
   child agents based on which node's capabilities are needed
3. Or write a custom MCP tool that wraps node selection logic

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

Nodes advertise capabilities at connect time. The node-host process can expose:

| Capability        | Description                |
| ----------------- | -------------------------- |
| `system.run`      | Shell command execution    |
| `camera.snap`     | Photo capture              |
| `canvas.navigate` | Browser/WebView navigation |
| `screen.record`   | Screen recording           |
| `location.get`    | GPS/location data          |

The default `openclaw node run` exposes all available capabilities. To restrict:

```bash
# Only expose shell execution, no camera/screen/etc.
openclaw node run --capabilities system.run
```

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

## References

- [OpenClaw Exec Tool docs](https://docs.openclaw.ai/tools/exec)
- [OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals)
- [OpenClaw Gateway Configuration](https://docs.openclaw.ai/gateway/configuration)
- [OpenClaw Gateway Protocol](https://docs.openclaw.ai/gateway/protocol)
- [OpenClaw Nodes](https://docs.openclaw.ai/platforms/nodes)
- [OpenClaw Node Troubleshooting](https://docs.openclaw.ai/platforms/nodes/troubleshooting)
- Our instance config: <../k8s/openclaw/openclawinstance.yaml>
- Sandbox RBAC: <../k8s/openclaw-sandbox/role-sandbox.yaml>
