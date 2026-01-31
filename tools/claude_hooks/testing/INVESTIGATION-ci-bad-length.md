# CI Failure Investigation: `[SSL: BAD_LENGTH]` in Mock Egress Proxy

## Incident

**CI Run**: [#21537117257](https://github.com/agentydragon/ducktape/actions/runs/21537117257/job/62064913670)
**Failing test**: `test_bazel_build_after_hook` in `tools/claude_hooks/test_session_start.py`
**Trigger commit**: `168cfe3` ("Add Chromium shared library deps to RBE worker image")
**Symptom**: Bazelisk download of ~46MB Bazel binary through mock egress proxy failed with `unexpected EOF`. Proxy logs showed `[SSL: BAD_LENGTH] bad length` after forwarding 45,695,525 bytes.

The trigger commit only changed `tools/rbe_image/Dockerfile` and had no relation to proxy code.

## Timeline

1. Test starts mock egress proxy, configures environment, runs `bazel build`.
2. Bazelisk attempts to download Bazel binary from `releases.bazel.build` (~46MB).
3. Download goes through: client -> mock egress proxy -> real egress proxy -> internet.
4. After ~45.7MB transferred, proxy logs: `SSL error for releases.bazel.build:443: [SSL: BAD_LENGTH] bad length (_ssl.c:2426)`
5. Client receives truncated data, bazelisk reports `unexpected EOF`.

## Hypothesis

The `_forward_bidirectional` method used non-blocking SSL sockets with `select()`:

```python
client_ssl.setblocking(False)
server_ssl.setblocking(False)
while True:
    readable, _, _ = select.select(sockets, [], sockets, 30.0)
    for sock in readable:
        try:
            data = sock.recv(65536)
            other.sendall(data)
        except ssl.SSLWantWriteError:
            continue  # <-- data from recv() is dropped here
```

When `sendall()` raised `SSLWantWriteError` (kernel send buffer full), the `continue` statement discarded the data that had already been read from the source socket. Per OpenSSL's contract, after `SSL_ERROR_WANT_WRITE` the caller must retry with the exact same buffer. Dropping it instead corrupts the TLS record stream — the peer sees a record whose length field doesn't match the actual data, producing `BAD_LENGTH`.

## Evidence

### Supporting

- The error (`BAD_LENGTH`) is consistent with TLS record stream corruption — a length field in the record header doesn't match the payload bytes that follow.
- The failure happened during a large (~46MB) one-directional transfer, which is the scenario most likely to fill kernel send buffers and trigger `SSLWantWriteError`.
- The failure is intermittent (CI was green before and after), consistent with a race condition that depends on kernel buffer state and scheduling.

### Against / Inconclusive

- **Cannot reproduce on loopback**: `SSLWantWriteError` requires network backpressure that doesn't occur on `127.0.0.1`. Loopback has effectively infinite bandwidth and very large kernel buffers.
- **Tests don't distinguish implementations**: All 6 bidirectional forwarding tests pass with both the old (select-based) and new (thread-based) implementations. This includes an 8MB download test and concurrent connection tests.
- **Attempted backpressure simulation**: Tried setting `SO_RCVBUF` to 4KB and adding artificial read delays. Tests still passed with the old implementation — loopback is too fast.
- **No smoking gun**: The CI failure was in a sandboxed RBE environment with real network hops. We cannot recreate the exact network conditions.

## Changes Made

### 1. Replaced `_forward_bidirectional` implementation

- **Old**: Non-blocking sockets + `select()` loop, single thread
- **New**: Thread per direction, blocking sockets with 30s timeout
- **Rationale**: Blocking `sendall()` handles backpressure correctly by blocking until all data is written, rather than raising `SSLWantWriteError`.

### 2. Added `verify_target_certs` parameter

- Allows tests to connect to local TLS servers with self-signed certs without `CERTIFICATE_VERIFY_FAILED`.

### 3. Removed `require_auth` parameter

- Auth is always required, matching the real egress proxy's behavior.

### 4. Added bidirectional forwarding test suite

- `test_small_echo`: Small payload round-trip
- `test_large_echo`: 1MB echo
- `test_large_download`: 8MB server-initiated send (simulates bazelisk download)
- `test_multiple_messages`: 10 send/recv cycles on one connection
- `test_concurrent_connections`: 5 simultaneous connections
- `test_server_closes_immediately`: Server closes right after TLS handshake

These tests verify correctness of the proxy's forwarding but **do not distinguish the old implementation from the new one** because the bug requires real network backpressure.

## Open Questions

1. **Was `SSLWantWriteError` actually the cause?** The hypothesis is plausible but unproven. The failure could also have been caused by a transient issue in the real upstream egress proxy, a gVisor networking quirk, or something else entirely.

2. **Why was this not seen before?** Possibly it was — the test may have been flaky for a while. Or the 46MB bazelisk download is new enough (or infrequent enough in CI) that the conditions hadn't aligned before.

3. **Will the thread-based implementation actually fix CI?** Unknown without more CI runs exercising the same code path under similar conditions.

## Conclusion

The fix is a reasonable defensive change — blocking sockets with threads are simpler and cannot drop data on backpressure. However, we cannot prove it addresses the root cause because the bug cannot be triggered in a unit test on loopback. The real validation will come from CI stability over subsequent runs.
