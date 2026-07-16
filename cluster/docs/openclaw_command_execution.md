# OpenClaw command execution

OpenClaw command execution is disabled. The deployed `OpenClawInstance` sets:

```yaml
tools:
  exec:
    security: deny
```

There is no node host, direct-exec sidecar, sandbox service-account bridge, or
Airlock plugin. OpenClaw cannot submit MCP tool calls to Airlock and Airlock does
not expose an MCP endpoint or operator-approval queue.

Haku Console owns the active risky-tool workflow: authenticated agents submit
tool calls there, policy can decide transparent cases, and an operator approves
the remaining calls before Haku executes them. See
<../../haku/console/README.md> and <../../haku/docs/security.md>.

Do not restore command execution by reusing Airlock. Any future OpenClaw command
surface needs its own threat model, declarative credentials and network policy,
auditable authorization contract, and explicit operator decision about whether
OpenClaw should be resumed at all.
