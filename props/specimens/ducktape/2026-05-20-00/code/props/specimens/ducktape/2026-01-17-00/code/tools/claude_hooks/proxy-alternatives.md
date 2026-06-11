# Bazel Proxy Authentication Alternatives

Analysis of alternatives to the local proxy approach for Claude Code web's authenticated TLS-inspecting proxy. See <README.md> for the main documentation.

## Current Approach: Local Proxy (`proxy.py`)

The current implementation runs a local async TCP proxy on `localhost:18081` that:

1. Receives unauthenticated CONNECT requests from Bazel/Bazelisk
2. Adds `Proxy-Authorization: Basic <base64(user:pass)>` header
3. Forwards to upstream proxy with credentials

**Why it works**: The proxy sends authentication preemptively in the initial CONNECT request, before any challenge.

## Why Native Java/Bazel Proxy Auth Fails

Testing revealed the upstream proxy has non-standard behavior:

```
$ CONNECT bcr.bazel.build:443 (no auth)
→ HTTP/1.1 401 Unauthorized
  www-authenticate: Bearer realm=""
```

Problems:

1. **Wrong status code**: Returns `401` instead of `407 Proxy Authentication Required`
2. **Wrong header**: Uses `www-authenticate` instead of `Proxy-Authenticate`
3. **Wrong scheme**: Advertises `Bearer` (but actually accepts `Basic`)

Java's `Authenticator` (which Bazel uses via `ProxyHelper.java:185-191`) only triggers on `407` + `Proxy-Authenticate`. Since the proxy returns `401` + `www-authenticate`, the Authenticator is never called.

**Proof**: Direct Basic auth to upstream works fine:

```java
// Sending Proxy-Authorization: Basic <creds> directly → HTTP/1.1 200 OK
```

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
- But Authenticator only triggers on `407 Proxy-Authenticate`, which this proxy doesn't send

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

- Would work without local proxy
- Fixes the root cause

**Cons**:

- Requires maintaining a Bazel fork
- Significant maintenance burden
- Must rebuild Bazel or wait for upstream acceptance

**Verdict**: Could be a long-term upstream fix, but not practical for immediate use.

### 6. Transparent/IP-Allowlisted Proxy ❌

**What**: Configure infrastructure to use transparent proxy without per-request auth.

**Why it fails**: Requires changes to Claude Code web infrastructure, not user-controllable.

### 7. Keep Local Proxy ✓ (Current)

**Pros**:

- Works with non-standard proxy behavior
- Handles credential refresh (JWT rotation)
- Minimal footprint (~200 lines Python)
- No Bazel patching required

**Cons**:

- Extra process to manage
- Complexity in session startup

## Complexity Reduction Options

While keeping the local proxy, we could simplify:

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

The local proxy approach is the **least complex viable solution** given:

1. Non-standard proxy authentication behavior (401 + www-authenticate + Bearer)
2. Java/Bazel's expectation of RFC-compliant proxy auth (407 + Proxy-Authenticate)
3. Need for preemptive authentication

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
