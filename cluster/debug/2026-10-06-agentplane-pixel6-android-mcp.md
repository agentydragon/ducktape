# Agentplane staging and Pixel 6 Android MCP (deferred)

This note preserves the integration investigation from PR [#9313](https://github.com/agentydragon/ducktape/pull/9313).
The Pixel 6 ActionGroup is deferred; the PR branch now contains only this note. On 2026-10-06 the
user decided to uninstall the phone MCP app for now. The phone's state after that decision is unknown.

## Last observed setup

The app was Android Remote Control MCP **v1.12.0**, upstream commit
[`3777403d`](https://github.com/danielealbano/android-remote-control-mcp/tree/3777403d148283c5a18a3e8122ff819da4eed808).
While installed, the phone was reachable over Nebula at `10.42.0.50:8080` (roster host `pixel6`).
OAuth discovery returned HTTP 200; unauthenticated `/mcp` initialization returned HTTP 401; an
authenticated MCP initialize and `tools/list` succeeded and returned 57 tools. At the time, both
`oauth_enabled` and `bearer_token_enabled` were on. The phone was configured to bind to the network,
start on boot, and use the `pixel6` tool-name slug.

PR #9313 originally added a staging ActionGroup, TCP 8080 egress to the phone's Nebula `/32`, and a
dedicated SOPS bearer Secret. Those changes were never merged or reconciled into the cluster. The
static bearer token was configured on the phone for that attempt; do not copy it from old PR history
or reintroduce it. Check/reset the phone's authentication settings if the app is installed again.

## OAuth compatibility findings

The Android app supports RFC 7591 client registration, public clients (`token_endpoint_auth_method:
none`), authorization code + PKCE S256, on-device approval, token exchange, and refresh tokens. Its
OAuth access tokens are accepted by `/mcp` independently of the static bearer toggle. Keeping
`oauth_enabled=true` and setting `bearer_token_enabled=false` makes the server OAuth-only while
still rejecting unauthenticated MCP calls. See the upstream [OAuth metadata](https://github.com/danielealbano/android-remote-control-mcp/blob/3777403d148283c5a18a3e8122ff819da4eed808/app/src/main/kotlin/com/danielealbano/androidremotecontrolmcp/mcp/oauth/OAuthMetadata.kt),
[redirect policy](https://github.com/danielealbano/android-remote-control-mcp/blob/3777403d148283c5a18a3e8122ff819da4eed808/app/src/main/kotlin/com/danielealbano/androidremotecontrolmcp/mcp/oauth/OAuthPolicy.kt),
and [OAuth routes](https://github.com/danielealbano/android-remote-control-mcp/blob/3777403d148283c5a18a3e8122ff819da4eed808/app/src/main/kotlin/com/danielealbano/androidremotecontrolmcp/mcp/oauth/OAuthRoutes.kt).

The redirect allowlist accepts the fixed Claude and ChatGPT callbacks, ChatGPT's connector callback
namespace, and `http://` loopback (`localhost`, `127.0.0.1`, or `[::1]`, with any port/path). It rejects
other HTTPS callback hosts, including Agentplane's
`https://agentplane-staging.allegedly.works/mcp-linkage/callback`. A DCR attempt using that Agentplane
callback returned `invalid_redirect_uri`; it did not create a client.

Agentplane's `McpOAuthServer` supports a configured `client_id` or shared CIMD, and handles PKCE code
exchange and refresh. The phone does not advertise `client_id_metadata_document_supported`, so shared
CIMD is not applicable; when resuming, register a fixed public client on the phone and configure its
returned client ID. The ID is not a secret. Agentplane's browser callback requires the operator's
existing browser session, and its Action Service exchanges the code directly with the phone over
Nebula. See `agentplane/action_service/mcp_linkage.py` and `agentplane/app/api.py`.

## Viable paths if resumed

1. **No phone-app change: loopback relay.** Register a client whose exact redirect URI is, for
   example, `http://127.0.0.1:8765/mcp-linkage/callback`. Configure the same `redirect_uri` in
   `McpOAuthServer`. Run a short-lived relay bound only to `127.0.0.1` on the workstation where the
   operator browser is running. It should receive the phone's `code` and `state`, then return a 302
   to `https://agentplane-staging.allegedly.works/mcp-linkage/callback` with those values. Keep the
   browser logged into Agentplane and on the Nebula-connected workstation for the flow. The Action
   Service still performs the token exchange with the original loopback redirect URI and its stored
   PKCE verifier. The operator approves the request on the phone.
2. **Direct Agentplane callback.** This needs the Android app's allowlist to add Agentplane's exact
   HTTPS callback. That likely means an upstream app change or a separately signed/reinstalled build;
   check update/signing constraints before choosing it.

Before adding the ActionGroup, recheck the phone app/version, Nebula address and reachability. Allow
Action Service egress only to the phone's current mesh `/32` on TCP 8080. Complete a real OAuth link,
confirm token refresh, and only then disable the phone's static bearer toggle. Agentplane Action
calls remain subject to its operator approval path.
