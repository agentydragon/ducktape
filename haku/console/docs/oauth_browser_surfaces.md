# OAuth browser surfaces

Haku's OAuth and Agent-enrollment browser pages share a visual language, but they do not all share
one runtime. The ownership boundary follows what must still work when authentication or application
bootstrap is the thing that failed.

| Surface                                                     | Renderer        | Reason                                                                                                                                                           |
| ----------------------------------------------------------- | --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Provider and MCP account-link results                       | Console SPA     | These flows start in an authenticated console, benefit from the shared design system, and can update the original tab through the existing console event stream. |
| Operator-login failure                                      | Backend HTML    | A working authenticated SPA session cannot be assumed when establishing that session failed.                                                                     |
| Agent enrollment, continuation, denial, and terminal errors | Backend HTML    | Enrollment is an isolated authorization ceremony with restrictive CSP and must work before the Agent connection exists.                                          |
| Authentik login and consent                                 | Authentik theme | Authentik owns these documents; Haku templates cannot render them.                                                                                               |
| Google and upstream-provider consent                        | Provider        | Haku cannot theme third-party authorization pages.                                                                                                               |

## Account-link result handoff

Provider and MCP callbacks still terminate on the backend: only the backend validates OAuth state,
exchanges the authorization code, and persists credentials. After that work, the callback stores a
short-lived result in Postgres. Provider callbacks redirect to
`/_console/oauth-result/<opaque-result-id>`. MCP callbacks return directly to
`/_console/settings?oauth_result=<opaque-result-id>`; the SPA consumes the result, removes the
parameter immediately, and announces success or failure while showing the affected settings.

The result:

- is bound to the Operator who completed the flow;
- expires after five minutes and is consumed once;
- contains the presentation status, title, and bounded message on the server, never in the URL;
- works across console replicas; and
- is fetched by the trusted SPA route, which renders normal Haku components in light or dark mode.

The opaque path is safe to retain in browser history: possession alone is insufficient without the
matching Operator session, and it carries no provider error text. Reloading a consumed result shows
the explicit unavailable state rather than replaying it.

Do not move the code exchange into the browser, return tokens to the SPA, or put provider error
descriptions in query parameters.

## Remaining backend-page consolidation

Backend ownership does not imply a different design. The remaining pages should use a shared,
CSP-nonce-compatible standalone Haku shell with the same surface, border, action, success, and error
tokens as the SPA. It should support light and dark color schemes without loading the SPA runtime.

The intended migration order is:

1. Introduce the shared standalone shell and status-page view model.
2. Move Agent continuation and denial onto that status page.
3. Render browser-facing enrollment terminal errors as HTML while leaving protocol and `/api`
   errors as JSON.
4. Make the interactive enrollment form inherit the shell and remove its duplicated CSS.
5. Move operator-login failure onto the same shell, preserving its backend-only retry path.
6. Align Authentik's separately owned theme where practical; do not attempt to imitate Google or
   other third-party consent documents.

Every visual state needs screenshot coverage in light and dark themes, including success, failure,
expired/already-viewed, denial, continuation, and a narrow viewport. Security tests remain separate:
they must continue to verify escaping, CSP, cache policy, referrer policy, session binding, expiry,
and single-use consumption.
