# Central GitHub API observation proxy

This is the central replacement for workstation interception proxies. It also
observes cloud-mediated GitHub work on Claude endpoints: a direct GitHub-only
filter would not cover that traffic. Do not reuse the agent-sandbox proxy's CA,
credentials, or network authorization boundary for workstation traffic.

## Transport and identity

`github-proxy.allegedly.works:8443` is the configured authenticated HTTPS forward
proxy endpoint. A dedicated TLS-only Gateway passes the stream through a
hostname-specific TLSRoute; an ordinary HTTPRoute is not assumed to forward
CONNECT requests. Port 8443 avoids changing the shared Gateway's listeners:
its wildcard HTTPS and exact TLS-passthrough listeners on port 443 already report
`ProtocolConflict` on the deployed Cilium version. See the
[upstream overlapping-listener report](https://github.com/cilium/cilium/issues/47590).

There are two distinct certificate roles:

- The public proxy endpoint uses a normal publicly trusted server certificate.
  Clients verify that certificate and hostname before sending proxy credentials.
- A dedicated interception CA signs inner-origin certificates. Its private key
  stays in the cluster. Only its verified public certificate is installed in the
  selected application's private trust store; no global browser trust change.

Each host has its own SOPS-backed Basic credential. This is an explicit
noninteractive proxy-authentication surface, not an Authentik browser UI.
The server authenticates before forwarding, accepts no plaintext credential
transport, and removes proxy credentials from both headers and flow metadata
before saving raw captures. Metrics use configured client IDs, not user-supplied
identity labels or arbitrary paths.

For clients without usable proxy authentication, a loopback-only transport
trampoline adds the credential and connects over verified TLS. It does not
intercept, capture, cache response content, hold an interception signing key, or
fall back to a direct origin connection. The normal Desktop profile and OAuth
callback identity must remain unchanged.

## Credential ownership

The SOPS sources are `secrets/wyrm2-credentials.sops.yaml` and
`secrets/rugged-credentials.sops.yaml` beneath this directory. Each is one Secret
whose `stringData.credentials.json` contains a single fixed-client-ID/password
mapping. Flux and the corresponding host's user identity consume that same file;
one host is not granted decryption of the other's credential. The central runtime
reads both mounted JSON files and rejects duplicate IDs. Secrets stay out of the
Nix store, command arguments, metrics and ordinary logs.

Client IDs are `wyrm2-desktop` and `rugged-desktop`. Each password is independently
generated from 32 random bytes, encoded as 64 lowercase hexadecimal characters.
These are credential labels, not process identities: an opt-in CLI sharing the
same host relay uses that host's credential too.
Home Manager reads `stringData/credentials.json` from the corresponding SOPS file
into a mode-0600 runtime secret. Publishing the encrypted source does not activate
the host relay. Rotate one host's source, reconcile the central Secret and reload
the corresponding host's runtime secret together; do not create a second copy.

## Migration gate and cleanup

The central runtime and host trampoline are separate changes.
Committed manifests are not evidence of a successful rollout. Preserve the working local
mitigation until all of these are verified on the actual central route:

1. Correct credentials and both TLS trust chains work; wrong or missing
   credentials, untrusted proxy TLS and unreachable upstream fail closed.
2. Real Desktop traffic is attributed to the selected host, the existing exact
   cloud batch-status block still works, and unrelated application traffic and
   OAuth callbacks work through the normal launch path.
3. Request metrics and incremental stream metadata arrive centrally; raw capture
   appends safely, remains private, and contains no proxy password metadata.

Then retire the previous local interception service/configuration, owned runtime
overrides, temporary package/profile bridges, unused local interception private
keys and owned Nix GC roots. Inspect each exact target before cleanup and retain
operator replacements. Preserve raw investigation captures and the Desktop
profile/sign-in state. Do this per host: a source opt-in alone does not prove
rugged or wyrm2 migrated successfully.

Successful migration is not resolution of quota exhaustion. Account-wide quota
and observation coverage still require the agreed multi-day acceptance window.

## Central deployment

Root Flux owns `app/` through the `github-api-proxy` Kustomization, after the
identity/credentials, registry credentials, storage, monitoring, Gateway and
Reloader dependencies. The Deployment starts from a verified published runtime
image; Forgejo image automation tracks subsequent devel releases. Verify the
reconciled revision, actual image digest, Pod startup, individual Gateway listener
and TLSRoute conditions, and the authenticated route before migrating a client.

Capture storage uses the replicated `seaweedfs-ovh` class and an initial 100-GiB
claim. The app must run one writer with a Recreate rollout strategy. The PVC is
excluded from Flux pruning: removing the app must not delete investigation
evidence. There is no automatic capture rotation or deletion; inspect storage use
and arrange explicit retention before unattended long-term operation.

Only the TLS proxy port is routed publicly. A PodMonitor targets the separate
metrics port directly so readiness failure does not remove it from observation.
The runtime disables readiness after a capture write failure; do not configure
a liveness probe that restarts and clears this evidence-loss signal. Investigate
storage and incomplete captures before a controlled restart. Established
connections are not terminated by a readiness change; the exact-route mitigation
and metrics remain active. The rule set separately detects capture-write errors,
failed scrapes, missing targets, a high collection physical storage budget and
missing budget inputs. The budget compares deduplicated SeaweedFS normal-volume
bytes (including replicas) with the PVC storage request; it does not measure free
space or guarantee writes. Partial volume-server telemetry loss can undercount
it. See the [storage metric investigation](../../../debug/github_graphql_exhaustion/central_capture_storage.md)
for accounting and coverage limits. Validate retained metrics and notification
delivery during rollout, not just rule installation.

Reloader watches the mounted configuration and Secrets. Certificate renewal or
credential rotation therefore causes a Recreate rollout; capture appends across
that restart. One writer is intentional. The image's Python launcher needs a
writable filesystem; the Pod has no privilege escalation, capabilities or
ServiceAccount token, but does not claim a read-only root filesystem. Working
signing PEMs live in a small memory-backed volume, not the evidence PVC.

Egress is public HTTP/HTTPS plus cluster DNS, not arbitrary cluster access. The
runtime must also reject private/loopback destinations and pin validated DNS
answers; network policy alone cannot fence the Pod's own loopback. Our cluster
node identities are intentionally not allowed, so even public
`*.allegedly.works` origins are outside this proxy's egress scope.
