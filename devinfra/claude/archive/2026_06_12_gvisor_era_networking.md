# gVisor-Era Networking

Historical context for Claude Code web sessions before the Firecracker migration
(pre-2026-06) and before Anthropic dropped the explicit egress proxy. Nothing here
is current behavior; see <../README.md> § Networking for the current state.

## Explicit Egress Proxy

Earlier Claude Code web containers injected `HTTPS_PROXY=http://<container_id>:<jwt>@<host>:<port>`
into the process environment. The hook daemon previously ran a substantial
subsystem under `devinfra/claude/auth_proxy/` to handle it:

- Load the TLS inspection CA from `/usr/local/share/ca-certificates/swp-ca-production.crt`
- Build a Java truststore (`cacerts.jks`) for Bazel JVM's HTTPS
- Build a combined CA bundle (`combined_ca.pem`) for `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, etc.
- Start a UDS proxy (`UdsRemoteProxy`) that Bazel used via `--remote_proxy=unix:<path>`
  to work around gRPC-Java's `Authenticator` installation timing bug with HTTP
  CONNECT proxies.
- A BES interceptor that inspected Bazel build events and forwarded them to BuildBuddy.

Current containers reach the internet without any of that — outbound HTTPS to public
hosts just works, and Anthropic's CA is already in the system CA bundle if TLS is being
inspected somewhere upstream. The entire `auth_proxy/` subsystem was removed; see git
log for the removal. If Anthropic reverts to explicit proxy env vars, restore the
subsystem from history.
