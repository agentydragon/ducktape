# CCR Bazel truststore race (`bb`/`bbr` PKIX failures)

**Symptom.** In a Claude Code Remote (CCR) session, `bb`/`bbr` fail every repository
fetch with a TLS error, while `bazelisk` works:

```text
TLS error: (certificate_unknown) PKIX path building failed:
sun.security.provider.certpath.SunCertPathBuilderException:
unable to find valid certification path to requested target
```

**One-line fix.** Run <../heal_ccr_bazel_trust.sh> (already wired into
<../web_setup.sh>, Step 3b — a fresh session self-heals). It writes `/etc/bazel.bazelrc`
pointing Bazel's JVM at the system Java store.

## Why it happens

The CCR agent proxy (`http://127.0.0.1:46587`, `HTTPS_PROXY`) re-terminates outbound TLS
with its own CA (`CN=CCR Upstream Proxy CA`). Every tool must trust
`/root/.ccr/ca-bundle.crt`. For JVM tools the proxy's boot-time setup is supposed to:

1. build a managed truststore `/root/.ccr/java-truststore.p12` seeded from the system
   store, and
2. write `/etc/bazel.bazelrc` pointing Bazel's server JVM at a store that trusts the CA.

That seed **races dpkg's pending `ca-certificates-java` trigger** — both regenerate
`/etc/ssl/certs/java/cacerts` — and usually loses. Reconstructed from a real session's
`/tmp/claude-code.log` + `/var/log/dpkg.log`:

| Time (UTC, 03:13) | Event                                                                                                               |
| ----------------- | ------------------------------------------------------------------------------------------------------------------- |
| :27               | CCR agent-proxy starts; begins installing its CA into system trust                                                  |
| :34               | dpkg reprocesses the **pending `ca-certificates-java` trigger** → starts regenerating `/etc/ssl/certs/java/cacerts` |
| :37.480           | CCR `update-ca-certificates` **exits 1** ("falling back to env-var trust") — collided with the in-flight trigger    |
| :39.427           | `/etc/ssl/certs/java/cacerts` finally written                                                                       |
| :39.792           | CCR `keytool -importkeystore` (copy that store into the p12) **exits 1** — read it before it settled                |

Because the seed failed, CCR wrote **neither** `/root/.ccr/java-truststore.p12` **nor**
`/etc/bazel.bazelrc`. Consequences:

- **`bazelisk` still works** — the ducktape claude-hook independently points the _session_
  bazelrc at `/etc/ssl/certs/java/cacerts` (`--host_jvm_args=-Djavax.net.ssl.trustStore=…`),
  which post-boot is valid and carries the proxy CA.
- **`bb`/`bbr` break** — they do not read that session bazelrc, and with no
  `/etc/bazel.bazelrc` they fall back to the JDK-**embedded** cacerts, which lacks the
  proxy CA → PKIX failure.

**Proof it's a race, not a config error:** the exact failing commands **succeed once the
session is usable** (the store has settled by then):

```bash
# CCR's own seed command — fails at boot, succeeds now (151 entries):
keytool -importkeystore -noprompt -srckeystore /etc/ssl/certs/java/cacerts \
  -srcstorepass changeit -destkeystore /tmp/probe.p12 -deststoretype PKCS12 -deststorepass changeit
update-ca-certificates   # exit 1 at boot, exit 0 now
```

The pending `ca-certificates-java` trigger fires on first boot because the image is a
snapshot with the trigger left pending; it runs concurrently with CCR's init. This is an
**upstream (Anthropic/CCR) race** — worth reporting — but cheap to self-heal.

## The heal

Since the system store `/etc/ssl/certs/java/cacerts` is valid and carries the proxy CA by
the time a session is usable, we complete the step CCR skipped: write `/etc/bazel.bazelrc`
(the system bazelrc, read by every Bazel invocation including `bb`/`bbr`) pointing the JVM
at it — the same store the session bazelrc already uses for `bazelisk`.

<../heal*ccr_bazel_trust.sh> does this idempotently and only when `/etc/bazel.bazelrc` is
absent (CCR writes it on a \_successful* seed), so a healthy session is never clobbered and a
non-CCR machine is a no-op. It is invoked from <../web_setup.sh> so fresh sessions self-heal.

**Detect** a broken session:

```bash
curl -sS http://127.0.0.1:46587/__agentproxy/status | grep java_truststore_seed_failed
test -e /etc/bazel.bazelrc || echo "no /etc/bazel.bazelrc — bb/bbr will PKIX-fail"
```

## Scope / non-goals

- **`bazelisk` is unaffected** — its session bazelrc already trusts the proxy CA.
- **The residual `403 Forbidden`** on _direct_ `github.com` archive fetches (e.g. a local
  `bb run` that needs an un-cached repo) is a **separate, by-design egress policy**, not
  this TLS bug — the repo fetches through the BuildBuddy remote cache / RBE (`bbr`), which
  is why `bbr build`/`test` pass once TLS is healed. Do not route around the 403; see
  `/root/.ccr/README.md`.
