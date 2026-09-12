# External MCP client metadata

Actions uses FastMCP 3.4.4's built-in CIMD support alongside DCR. Discovery advertises
`client_id_metadata_document_supported: true` and public-client token authentication
(`none`); Claude requires both before selecting CIMD.

A fetched client document does not authorize an Identity, create an active Connection,
or bypass operator consent. CIMD authorization traverses the same enrollment, PKCE,
upstream-principal mapping, canonical grant activation, refresh, and revocation paths
as a dynamically registered client.

The pinned FastMCP fetcher validates HTTPS metadata URLs, pins DNS resolution, rejects
private/loopback/link-local addresses, disables redirects, and bounds response size
and time. No custom HTTP fetcher or relaxed TLS/SSRF checks are introduced. The
staging/testing Actions network policies add only `claude.ai` on TCP 443 with matching
TLS SNI, for `https://claude.ai/oauth/mcp-oauth-client-metadata`. Other client metadata
origins require separately reviewed network permissions; enabling CIMD is not an
unrestricted egress grant.

The HTTP/PostgreSQL regression replaces only the external metadata fetch and verifies
canonical consent/token/revocation behavior; private-address rejection exercises the
maintained fetch path without a replacement. These tests are not Claude.ai connection
proof. Testing still needs its external Actions OAuth provider configured before
external DCR or CIMD can be exercised there end to end.
