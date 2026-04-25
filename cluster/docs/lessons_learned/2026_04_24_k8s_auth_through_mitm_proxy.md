# Claude Code web → apiserver: client cert auth dies at the egress proxy

**Date**: 2026-04-24
**Status**: Fixed — migrated to Authentik-issued OIDC JWT (see the
`kubectl-sandbox-client-credentials` Authentik provider + `claude-jwt-rotation`
CronJob + `kubeapi.allegedly.works` HTTPRoute).

## Symptoms

- From a Claude Code web sandbox, `kubectl get pods -n claude-sandbox`
  against the `claude-web-k8s-cert.yaml`-based kubeconfig failed with:

  ```text
  Error from server (ServiceUnavailable): the server is currently unable to
  handle the request
  ```

  and kubectl `-v=8` showed Envoy's `503 upstream connect error ... TLS_error:
... CERTIFICATE_VERIFY_FAILED: verify cert failed: X509_verify_cert:
certificate verification error at depth 0: unable to get local issuer
certificate:TLS_error_end`.

- Same kubeconfig worked fine from a laptop (no proxy in the way).
- The `kubectl-local` MCP server had also been crashing on every CC session
  startup, but with an unrelated error
  (`RuntimeError: refusing to overwrite /tmp/claude-sandbox-kc.XXXXXX`) that
  was masking the network-side failure.

## Root Cause

Two distinct bugs, one primary and one latent.

### Primary: Anthropic's egress proxy is an L7 TLS-terminating MITM

Every `*.allegedly.works` host (and every other HTTPS destination) presents
this cert when accessed from a CC web sandbox:

```console
$ openssl s_client -connect api.allegedly.works:443 </dev/null \
    | openssl x509 -noout -issuer -subject
issuer=O=Anthropic, CN=sandbox-egress-production TLS Inspection CA
subject=CN=*.allegedly.works
```

github.com, buildbuddy.io, cache.nixos.org — same Anthropic issuer.
NODE_EXTRA_CA_CERTS ships the Anthropic CA, so clients don't notice the
interception; it's a transparent L7 MITM.

Consequence: when kubectl does a TLS handshake, it's talking to the proxy,
not to `api.allegedly.works`. Two independent breakages fall out of this:

1. **kubectl's x509 client cert never reaches upstream.** It's in the
   client↔proxy handshake. The proxy terminates that TLS session, then
   opens a fresh upstream session as the client — without the cert — so the
   apiserver never sees it. Client-cert auth is structurally incompatible
   with an L7 TLS-terminating proxy.
2. **Upstream cert validation fails.** The proxy's upstream TLS handshake
   goes to the apiserver via `api.allegedly.works`, which presents a
   cluster-CA-signed cert (passthrough via the TLSRoute in
   `cluster/k8s/kube-api-proxy/tlsroute.yaml`). The proxy validates against
   public CAs, fails, returns 503 to the client. This is the error we saw.
   Even if we'd "fixed" issue 1, issue 2 would still 503 us.

### Latent: `kubectl-local-mcp.sh` clobber-protection crash

`mktemp` creates a zero-byte file. `write_kubeconfig.py`'s safe-write path
at `devinfra/claude/scripts/write_kubeconfig.py` reads that file, parses it
as YAML (gets `None`), compares to the new kubeconfig dict, and raises
`refusing to overwrite …: existing kubeconfig differs from the one we'd
write`. Every session startup. Fixed by switching to `mktemp -u` which
reserves a name without creating the file.

## Why client cert worked before

It didn't — not on CC web. Client-cert auth works from laptops because
they reach `api.allegedly.works` directly over the LAN (no Anthropic proxy),
TLS-passthrough on the Cilium Gateway forwards the raw TLS to the apiserver,
and the apiserver terminates the TLS, reads the client cert, and maps its
`O=oidc-ksbx-groups:kubectl-sandbox-users` field to a K8s Group. End-to-end,
no interception. CC web is the new dimension here; client-cert auth was
never compatible with it.

The cert migration in commit `ff3ac18e0` (2026-04-18) replaced an older
ServiceAccount-token setup with this cert setup, specifically to **eliminate
the `claude-code-web` SA subject from ~30 RoleBindings** by having the cert's
`O=` field resolve to the existing `oidc-ksbx-groups:kubectl-sandbox-users`
Group at authentication time. That constraint (identity → Group at one
mapping layer, no per-binding edits) is the non-negotiable part; the new
token design has to preserve it.

## Fix

Reach the apiserver via a **publicly-trusted** LE cert, and use a bearer
JWT whose `groups` claim resolves to the existing Group through the already-
deployed apiserver `AuthenticationConfiguration`.

### 1. Second apiserver route with LE cert termination

Added `kubeapi.allegedly.works` (`cluster/k8s/kube-api-proxy/httproute.yaml`)
on the existing `https-wildcard` listener. Cilium Gateway terminates the
wildcard LE cert, then re-encrypts to `kubernetes:443` via a
`BackendTLSPolicy` that validates the apiserver cert against the cluster
root CA (auto-mounted `kube-root-ca.crt` ConfigMap in `default`).

The Anthropic proxy's upstream validation now sees a publicly-trusted cert,
passes, and the proxy relays the HTTP request (with the Authorization
header intact). The legacy `api.allegedly.works` TLSRoute stays — laptops
still use it.

### 2. Non-interactive OIDC JWT via Authentik client_credentials

We already ran `kubectl-sandbox-mcp` for the interactive OAuth case — its
`kubectl_sandbox_fixed_groups` scope mapping hardcodes
`groups: ["kubectl-sandbox-users"]` on issued JWTs regardless of caller.
The fix reuses that scope mapping on a new OIDC provider
(`kubectl-sandbox-client-credentials`, `client_type = "confidential"`) with
a `client_credentials` grant so machine identities can mint JWTs without
user consent. A 4th JWT issuer entry in the apiserver's
`AuthenticationConfiguration` (`cluster/terraform/main/infrastructure.tf`)
maps that provider's `groups` claim to the same
`oidc-ksbx-groups:kubectl-sandbox-users` Group as every other sandbox path.

**Zero RoleBinding edits.** Same constraint the cert migration enforced,
same solution shape — map identity to Group at one layer, don't duplicate.

### 3. In-cluster rotation, JWT-over-SOPS to git

`cluster/k8s/agents/claude-jwt-rotation/` (replaces `claude-cert-rotation/`)
runs biweekly: `curl` at the Authentik token endpoint with the confidential
client_id + client_secret mounted from an in-cluster Secret, jq-extracts
`.access_token`, commits it SOPS-encrypted to `secrets/claude-web-k8s-jwt.yaml`.
`write_kubeconfig.py` decrypts and embeds it as `user.token` at SessionStart.

Cadence: cron every 15 days, token validity 45 days. Worst-case session
starts 15 days after the last successful rotation → still 30 days of
remaining validity.

### 4. client_secret stays in-cluster

The long-lived credential (`client_id` + `client_secret` of the Authentik
confidential client) lives in a K8s Secret in `agents-infra`, mounted only
into the CronJob pod. CC web sandboxes never see it — they only handle
the already-minted JWT.

## What We Learned

- **L7 TLS-terminating MITM kills client-cert auth for all destinations.**
  Not a "bad cert" problem, not a "proxy allowlist" problem — a structural
  one. If there's any downstream that needs client certs through the
  Anthropic proxy, it needs a different auth method.
- **Distinguish "upstream cert validation" from "downstream cert relay".**
  The proxy rejects `api.allegedly.works` because the cluster CA isn't
  public; that's upstream validation. Moving upstream to an LE cert fixes
  _that_, but the client cert is a separate L7 thing that still dies at
  the proxy. We needed both fixes (LE hostname + bearer auth), not one.
- **Identity-to-Group mapping at the authn boundary is a non-negotiable
  invariant.** Two different migrations (token → cert, cert → token) have
  both converged on "claim/field → Group via a single apiserver-side
  mapping". Adding SA subjects to every binding (which the pre-2026-04-18
  token flow did) is a 30-binding surface that stops scaling.
- **`kubectl-sandbox-mcp`'s `kubectl_sandbox_fixed_groups` scope mapping
  is reusable** for any Authentik OAuth2 provider that wants to issue
  sandbox-scoped JWTs. Don't rebuild it; attach it.
- **`mktemp` creates the file.** `mktemp -u` doesn't. `write_kubeconfig.py`'s
  "refuse to clobber" check turned that into a latent crash that was
  masking the real network failure. Always match tempfile lifecycle to
  downstream consumer expectations.
- **Verification tool that actually nailed the root cause:** `openssl
s_client -connect <host>:443 | openssl x509 -noout -issuer`. If the
  issuer is the environment's MITM CA, you're not talking to who you think.

## References

- Fix commit: (this branch, claude/fix-kubectl-auth-4qjkm)
- Pre-existing Authentik scope mapping:
  `tf/gitops/agent-machine-access/main.tf` `kubectl_sandbox_fixed_groups`
- Apiserver `AuthenticationConfiguration`:
  `cluster/terraform/main/infrastructure.tf`
- Gateway API routes: `cluster/k8s/kube-api-proxy/`
- JWT rotation CronJob: `cluster/k8s/agents/claude-jwt-rotation/`
- Kubeconfig writer: `devinfra/claude/scripts/write_kubeconfig.py`
- Prior migration: commit `ff3ac18e0` (2026-04-18, token → cert,
  "eliminated the claude-code-web SA subject from ~30 RoleBindings")
- Background: `cluster/docs/mcp_oauth_authentik_notes.md`
