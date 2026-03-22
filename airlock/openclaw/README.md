# airlock OpenClaw plugin

OpenClaw plugin that bridges the parent [airlock](../) MCP proxy
and provides the exec tool via a DirectExecServer sidecar.

## What it does

1. **Exec tool**: Registers an `exec` tool backed by the DirectExecServer sidecar
   (pod-local, unauthenticated). Injects `OPENCLAW_SESSION_ID` into the command env.
2. **Airlock notifications**: Subscribes to session log HWM resources, delivers
   terminal action results (approved/denied/withdrawn) via `enqueueSystemEvent`.
3. **Airlock tools** (optional, `registerTools: true`): Re-registers airlock-wrapped
   tools with OpenClaw, injecting `session_key` automatically.

## Configuration

Add to your OpenClaw config:

```jsonc
{
  "plugins": {
    "entries": {
      "airlock": {
        "config": {
          "airlock": {
            "url": "http://airlock.airlock.svc.cluster.local:8765/mcp",
            "token": "<token from Airlock>",
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
(<../../openclaw/Dockerfile>). The image installs plugin deps at build time.

To install manually:

```bash
cd airlock/openclaw
npm install
```

Then add the plugin to your OpenClaw config pointing at the plugin directory.
