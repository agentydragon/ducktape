# gVisor-Era Networking and Supervisor Workarounds

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

## 9p Filesystem: No Unix Socket Hard Links (origin of the TCP supervisor port)

**Affected**: gVisor-era Claude Code web sandbox, where root `/` was 9p. Current
sessions run on Firecracker microVMs with an ext4 root, so the trigger no longer
exists — recorded because the TCP-socket configuration below is still what ships.

**Root cause**: Supervisord uses hard links for atomic Unix socket creation (`link()`
syscall). The 9p filesystem doesn't support hard linking Unix domain sockets, returning
`EOPNOTSUPP` (errno 95). When the hard link fails, supervisord misinterprets this as a
stale socket and enters an infinite retry loop.

**Solution**: TCP socket (`inet_http_server`) on `127.0.0.1:19001` instead of a Unix
socket (`DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT` overrides the port).

## Python Container-Runtime Files

The retired Python daemon kept supervisor state in `<session_dir>/supervisor/`:
`supervisord.conf`, `supervisord.{log,pid}`.
