# approval-gate OpenClaw plugin

OpenClaw plugin that bridges the [approval gate](../../approval_gate/) MCP proxy
and provides the exec tool via a DirectExecServer sidecar.

## What it does

1. **Exec tool**: Registers an `exec` tool backed by the DirectExecServer sidecar
   (pod-local, unauthenticated). Discovers the tool schema from the sidecar at
   startup and injects `OPENCLAW_SESSION_ID` into the command environment.
2. **Approval gate notifications**: Connects to the approval gate MCP server,
   subscribes to session log HWM resources, and delivers terminal action results
   (approved/denied/withdrawn) to the agent via `enqueueSystemEvent`.
3. **Approval gate tools** (optional, `registerTools: true`): Discovers and
   re-registers approval-gate-wrapped tools with OpenClaw, injecting `session_key`
   automatically.

## Configuration

Add to your OpenClaw config:

```jsonc
{
  "plugins": {
    "entries": {
      "approval-gate": {
        "config": {
          "approvalGate": {
            "url": "http://approval-gate.approval-gate.svc.cluster.local:8765/mcp",
            "token": "<token from the approval gate>",
          },
          "execServer": {
            "url": "http://127.0.0.1:8766/mcp",
          },
        },
      },
    },
  },
}
```

## Installation

The plugin is included in the custom OpenClaw Docker image
(`docker/openclaw/Dockerfile`). The image installs plugin deps at build time.

To install manually:

```bash
cd openclaw/approval-gate
npm install
```

Then add the plugin to your OpenClaw config pointing at the plugin directory.
