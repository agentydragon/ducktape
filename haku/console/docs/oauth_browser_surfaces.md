# OAuth browser surfaces

The Console no longer accepts MCP OAuth authorization or callback requests and no longer serves
the browser Agent-enrollment ceremony those requests initiated. Existing Agent and OAuth credential
records remain durable; Operators can inspect Agents and change eligible access profiles in Settings.

| Surface                 | Owner           | Reason                                                                                            |
| ----------------------- | --------------- | ------------------------------------------------------------------------------------------------- |
| Operator-login failure  | Console backend | A working authenticated Operator session cannot be assumed when establishing that session failed. |
| Authentik login/consent | Authentik theme | Authentik owns these documents; Haku templates cannot render them.                                |

Operator-login failure remains a small backend-rendered exception because the Operator session it
would use has failed to establish. Authentik documents remain owned by that service; align its
separately owned theme where practical, but do not attempt to imitate third-party consent documents.
