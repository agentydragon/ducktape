# Bazel Auth Proxy Alternatives

Analysis of alternatives to the auth proxy approach for Claude Code web's authenticated TLS-inspecting proxy. See <README.md> for the main documentation.

## Current Approach: Auth Proxy (`proxy.py`)

The current implementation runs an auth proxy on `localhost:18081` that:

1. Receives unauthenticated CONNECT requests from Bazel/Bazelisk
2. Adds `Proxy-Authorization: Basic <base64(user:pass)>` header
3. Forwards to upstream proxy with credentials

**Why it works**: The proxy sends authentication preemptively in the initial CONNECT request, before any challenge.

## Why Native Java/Bazel Proxy Auth Fails

The upstream proxy returns RFC-compliant proxy auth challenges (verified 2026-03-16):

```
$ CONNECT bcr.bazel.build:443 (no auth)
→ HTTP/1.1 407 Proxy Authentication Required
  proxy-authenticate: Basic realm="proxy"
  server: envoy

$ CONNECT bcr.bazel.build:443 (with Proxy-Authorization: Basic <creds>)
→ HTTP/1.1 200 OK
```

Despite correct 407 + `Proxy-Authenticate: Basic`, Java/Bazel native auth still fails for two reasons:

1. **Java doesn't read `HTTPS_PROXY` env vars**: Java uses system properties (`-Dhttps.proxyHost`/`-Dhttps.proxyPort`), so Bazel never points its CONNECT at the right proxy host
2. **Basic auth disabled for HTTPS tunneling**: Since Java 8u111, `jdk.http.auth.tunneling.disabledSchemes=Basic` blocks Basic auth for CONNECT tunneling even with a valid 407 challenge

## Alternative Approaches Evaluated

### 1. Native JVM Proxy Settings ❌

**What**: Set JVM properties for proxy auth:

```
-Dhttps.proxyHost=proxy.host
-Dhttps.proxyPort=port
-Djdk.http.auth.tunneling.disabledSchemes=
```

**Why it fails**:

- `http.proxyUser`/`http.proxyPassword` are Apache HTTP client properties, not standard Java
- Java's `HttpURLConnection` uses `Authenticator.setDefault()` which Bazel does set
- Authenticator triggers on `407 Proxy-Authenticate` (which the proxy now correctly sends), but Basic auth for HTTPS tunneling is disabled by default since Java 8u111

**Source**: Bazel's `ProxyHelper.java` uses `Authenticator.setDefault()` at lines 185-191.

### 2. Bazel Credential Helpers ❌

**What**: External binary that provides credentials for remote services.

**Why it fails**: Credential helpers are for endpoint authentication (Authorization header), not proxy authentication (Proxy-Authorization header). They're designed for:

- Remote cache/execution services
- External repositories
- Build event streams

Not for HTTPS proxy tunneling.

**Source**: [Bazel Credential Helpers Proposal](https://github.com/bazelbuild/proposals/blob/main/designs/2022-06-07-bazel-credential-helpers.md)

### 3. .netrc File ❌

**What**: Store credentials in `~/.netrc` for `http_archive` rules.

**Why it fails**: Same as credential helpers - for endpoint auth, not proxy auth.

### 4. Pre-fetch with --distdir ⚠️

**What**: Download all dependencies manually, use `--distdir=/path` to tell Bazel to use local copies.

**Pros**:

- No proxy complexity at build time
- Works offline

**Cons**:

- Impractical for development (need to pre-fetch ALL transitive deps)
- Breaks `bazel mod` and BCR resolution
- Must update distdir when deps change

**Verdict**: Only viable for air-gapped environments, not active development.

### 5. Patch Bazel to Support Preemptive Proxy Auth ⚠️

**What**: Modify `ProxyHelper.java` to set `Proxy-Authorization` via `setRequestProperty()` instead of relying on `Authenticator`.

**Pros**:

- Would work without auth proxy
- Fixes the root cause

**Cons**:

- Requires maintaining a Bazel fork
- Significant maintenance burden
- Must rebuild Bazel or wait for upstream acceptance

**Verdict**: Could be a long-term upstream fix, but not practical for immediate use.

### 6. Transparent/IP-Allowlisted Proxy ❌

**What**: Configure infrastructure to use transparent proxy without per-request auth.

**Why it fails**: Requires changes to Claude Code web infrastructure, not user-controllable.

### 7. Keep Auth Proxy ✓ (Current)

**Pros**:

- Works around Java's env var and Basic auth tunneling limitations
- Handles credential refresh (JWT rotation)
- Minimal footprint (~200 lines Python)
- No Bazel patching required

**Cons**:

- Extra process to manage
- Complexity in session startup

## Complexity Reduction Options

While keeping the auth proxy, we could simplify:

### A. Eliminate Credential File Refresh

If JWT tokens don't rotate during a session, we could:

- Read credentials once at startup
- Remove file-watching logic
- Reduces ~30 lines of code

### B. Use systemd User Service (if available)

- Move proxy management out of session hook
- Simplify lifecycle management

### C. Combine with Bazelisk Wrapper

Instead of separate proxy + wrapper, single binary that:

- Handles proxy auth
- Wraps Bazel invocations
- Reduces component count

## Conclusion

The auth proxy approach is the **least complex viable solution** given:

1. Java doesn't read `HTTPS_PROXY` env vars (uses JVM system properties instead)
2. Basic auth for HTTPS tunneling disabled by default since Java 8u111
3. Need for preemptive authentication (credential hot-reload for JWT rotation)

The only alternatives that could work require:

- Infrastructure changes (transparent proxy) - not user-controllable
- Bazel source patches - high maintenance burden

## References

- [Bazel Issue #14675](https://github.com/bazelbuild/bazel/issues/14675) - Authenticated HTTPS proxy
- [Bazel Issue #26674](https://github.com/bazelbuild/bazel/issues/26674) - Build behind proxy (2025)
- [Bazel Issue #601](https://github.com/bazelbuild/bazel/issues/601) - Work behind a proxy
- [Bazel ProxyHelper.java](https://github.com/bazelbuild/bazel/blob/master/src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/ProxyHelper.java)
- [JDK-8210814](https://bugs.openjdk.org/browse/JDK-8210814) - Cannot use Proxy Authentication with HTTPS
- [Atlassian KB](https://confluence.atlassian.com/kb/basic-authentication-fails-for-outgoing-proxy-in-java-8u111-909643110.html) - Java 8u111 proxy auth changes
