# OAuth browser surfaces

Haku-owned Agent-enrollment pages belong to the trusted Console application when an Operator
session is available. The ownership boundary follows which component holds the authority for an
action, not whether the Agent being enrolled has connected yet.

| Surface                                                     | Target renderer       | Reason                                                                                                                 |
| ----------------------------------------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Agent enrollment, continuation, denial, and terminal errors | Console SPA, Settings | Haku Console owns Agent authority and the Operator session. Enrollment does not depend on the future Agent connection. |
| Operator-login failure                                      | Backend HTML          | A working authenticated Operator session cannot be assumed when establishing that session failed.                      |
| Authentik login and consent                                 | Authentik theme       | Authentik owns these documents; Haku templates cannot render them.                                                     |

## Agent enrollment in Settings

Settings should normally contain an Agents section listing the Operator's authorized Agents.
Lifecycle state and last-seen activity must remain distinct: Haku cannot infer that an MCP client is
currently connected merely because its Agent is active or was seen recently.

The public `/auth/agent-enrollment/<interaction-id>` endpoint remains the protocol entry point. It
establishes the browser binding and redirects through Operator login when needed, then enters the
trusted SPA at `/_console/settings/agents/enroll/<interaction-id>`. The browser renders the pending
create, reconnect, or deny decision within Settings. A local terminal result returns to the normal
Agents section with an announcement; an allow decision that must continue upstream navigates the
browser onward through the existing authorization flow.

The SPA owns presentation only. The backend continues to own the interaction, Operator and browser
binding, expiry, form/CSRF validation, Agent creation or reconnection, authorization decision, and
protocol continuation. This trusted Console route does not render inside or disclose enrollment
state to the sandboxed, cross-origin Haku UI frame.

The enrollment view model and actions are exposed only through same-origin Console APIs. The
Settings migration removed the duplicate Jinja enrollment, continuation, and denial surfaces.

## Remaining non-SPA surfaces

Operator-login failure remains a small backend-rendered exception because the Operator session it
would use has failed to establish. Authentik documents remain owned by that service; align its
separately owned theme where practical, but do not attempt to imitate third-party consent documents.

Every visual state needs screenshot coverage in light and dark themes, including denial,
continuation, and a narrow viewport. Security tests remain separate: they must continue to verify
escaping, CSP, cache policy, referrer policy, session binding, expiry, and single-use consumption.
