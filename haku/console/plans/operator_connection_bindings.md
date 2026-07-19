# Operator connection bindings

## Direction

Haku Console has two different backend shapes and must model them separately:

- A **remote MCP backend** is an HTTP MCP endpoint. Its `auth` configuration authenticates the
  console's MCP client transport for discovery and execution.
- An **in-process backend** is reviewed Python code registered in the console process. It has no
  authenticated MCP hop. Its optional `credential` selects a source that the implementation
  consumes only when executing a tool.

An **operator connection** is a separately consented external-account grant owned by one Operator.
Deployment config gives the connection a stable logical name and maps it to an OAuth provider. An
in-process backend binds to that name; the implementation registry declares which credential kind it
accepts. Startup rejects missing implementations, unknown connection names, and incompatible
credential bindings.

The **operator login identity** is different: it is the Operator's authenticated Haku Console
session identity and its stored Authentik token. `operator_login_identity` lets hostexec exchange that
token for a narrow, short-lived host token during execution. It is not a configurable external
account association and is never accepted by the Gmail or Calendar implementations.

The acting Agent's bearer authenticates only the outer `/mcp` request and resolves its canonical
Operator. It is never a backend credential. Likewise, an operator connection token is never MCP
transport authentication for an in-process backend.

## Current configuration

The existing combined Google grant remains stored under provider `google`. Config names that grant
`google_workspace`, and both built-in Google implementations bind to it. This changes no persisted
token key and requires no reconnect. Until storage is migrated, startup rejects two logical
connections mapped to the same provider so they cannot silently alias one token row.

```yaml
operator_connections:
  google_workspace:
    provider: google

mcp:
  servers:
    - id: gmail
      backend:
        kind: in_process
        credential:
          kind: operator_connection
          connection: google_workspace
    - id: google_calendar
      backend:
        kind: in_process
        credential:
          kind: operator_connection
          connection: google_workspace
```

Credential-free schema reflection uses the registered in-process implementation without resolving or
refreshing a token. Execution resolves the configured connection immediately before building the
per-call implementation.

## Future Google split

If Gmail, Calendar, Drive, or other Google surfaces later need separate consent grants, define
separate logical connections and rebind their implementations in config. At that point the provider
connection tables must key rows by `(operator_id, connection_name)` rather than
`(operator_id, provider)`, allowing several Google grants. The forward migration must map the existing
`google` row to the chosen connection name and preserve its access token, refresh token, scope,
revision, and timestamps; it must never require reconnect merely because the storage key changed.

Provider is the OAuth protocol/vendor descriptor. Connection name is the deploy-defined role of one
grant. `connection_id` remains the UUID of an actual persisted Operator association; it is not the
configuration name or type.
