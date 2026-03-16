# Bazel Proxy Auth: Alternative Approaches

Analysis of alternatives to the auth proxy for Claude Code web's authenticated
TLS-inspecting proxy. See <../README.md> for the current approach and rationale.

## Why the Auth Proxy Exists

The auth proxy exists specifically because of **gRPC-Java's proxy authentication
timing problem**. Bazel has two proxy-dependent subsystems:

1. **BCR fetches** (Java HTTP via `ProxyHelper`): `ProxyHelper` reads `HTTPS_PROXY`
   from `--repo_env`, installs a `java.net.Authenticator`, and handles 407 challenges.
   This works natively with `-Djdk.http.auth.tunneling.disabledSchemes=`.

2. **gRPC remote execution** (gRPC-Java/Netty): `ProxyDetectorImpl` uses
   `ProxySelector.getDefault()` (reads `-Dhttps.proxyHost`/`-Dhttps.proxyPort`) and
   calls `Authenticator.requestPasswordAuthentication()`. But `ProxyHelper` only
   installs the `Authenticator` when a repository rule downloads something — which
   may happen *after* the gRPC channel is already created.

The local auth proxy decouples the auth timing: gRPC connects to unauthenticated
`localhost:18081` immediately; the proxy handles credentials when forwarding.

## Alternatives Evaluated

### Native JVM Proxy Settings (partial — BCR only) ⚠️

Setting `-Dhttps.proxyHost`, `-Dhttps.proxyPort`, and
`-Djdk.http.auth.tunneling.disabledSchemes=` with `--repo_env=HTTPS_PROXY`
works for BCR fetches but not for gRPC remote execution due to the
`Authenticator` timing issue described above.

**This was attempted and reverted** — BCR worked, but `--remote_executor`
to `remote.buildbuddy.io` failed with "Unable to resolve host".

### Bazel Credential Helpers ❌

Credential helpers are for endpoint authentication (`Authorization` header), not
proxy authentication (`Proxy-Authorization`). Designed for remote cache/execution
services and external repositories — not HTTPS proxy tunneling.

### .netrc File ❌

Same issue — for endpoint auth, not proxy auth.

### Pre-fetch with --distdir ⚠️

Download all dependencies manually, use `--distdir` for local copies. Only viable for
air-gapped environments. Impractical for active development: must pre-fetch all
transitive deps, breaks `bazel mod` and BCR resolution, must update on dep changes.

### Patch Bazel ⚠️

Modify `ProxyHelper.java` to install the `Authenticator` earlier (before gRPC
channel creation), or modify gRPC-Java's `ProxyDetectorImpl` to read credentials
from a different source. Could be a long-term upstream fix, but requires maintaining
a Bazel fork.

### Transparent/IP-Allowlisted Proxy ❌

Requires changes to Claude Code web infrastructure, not user-controllable.

### Auth Proxy ✓ (Current)

- Works for both BCR fetches and gRPC remote execution
- Handles credential refresh (JWT rotation via per-connection file read)
- Minimal footprint (~200 lines Python)
- No Bazel patching required
- Extra process to manage (supervised)

## References

- [Bazel Issue #14675](https://github.com/bazelbuild/bazel/issues/14675) - Authenticated HTTPS proxy
- [Bazel Issue #26674](https://github.com/bazelbuild/bazel/issues/26674) - Build behind proxy (2025)
- [Bazel Issue #601](https://github.com/bazelbuild/bazel/issues/601) - Work behind a proxy
- [Bazel ProxyHelper.java](https://github.com/bazelbuild/bazel/blob/master/src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/ProxyHelper.java)
- [gRPC-Java ProxyDetectorImpl.java](https://github.com/grpc/grpc-java/blob/master/core/src/main/java/io/grpc/internal/ProxyDetectorImpl.java)
- [JDK-8210814](https://bugs.openjdk.org/browse/JDK-8210814) - Cannot use Proxy Authentication with HTTPS
- [Atlassian KB](https://confluence.atlassian.com/kb/basic-authentication-fails-for-outgoing-proxy-in-java-8u111-909643110.html) - Java 8u111 proxy auth changes
