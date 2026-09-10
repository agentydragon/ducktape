# Agentplane Gateway Service experiment

## Actions follow-up, 2026-09-10

Post-rollout verification at 04:21:14 UTC: #6012 merged as
`ddc21cab93a4d51f5cae4a9c18e0272941051825`. Flux Actions reached Ready at
`c1352813058857705807474061dcf7c69c93a8f8`, and live policy generation 4
contains the scoped backend rule. From Actions' unchanged `10.244.4.48`
network namespace through Gateway Service `10.106.122.5:443`:

- Canonical SNI: Actions discovery and Actions JWKS both HTTP 200.
- Wrong SNI with canonical Host: both HTTP 403.
- Direct plaintext to Authentik `10.244.4.24:9000`: reset, no HTTP response.

TLS verification remained enabled. These were credential-free read-only
connectivity probes, not application acceptance or a BuildBuddy test run.
DNS remains unchanged.

The policy audit reproduced the same failure from Actions Pod
`agentplane-actions-6669c695b6-pqp8m` (`10.244.4.48`, Cilium endpoint 3578)
on `ovh-ns102453`. Credential-free discovery probes around 04:07 UTC retained
the canonical Host/SNI and TLS verification: remote node `147.135.39.176:443`
returned 200, Gateway Service `10.106.122.5:443` returned 403, and the local
node `147.135.37.175:443` reset TLS (errno 104).

The app's #6007 rule selects only the app. Actions also needs the narrowly
selected Authentik-server TCP 9000 rule with canonical SNI for Gateway Service
access. This does not change DNS or repair the node-IP TLS path.

After #6007 rolled out, app discovery/JWKS returned 200 through the Service,
wrong-SNI requests returned 403, and direct plaintext backend access reset.
Five alternating app TLS rounds yielded five Service successes, five remote
node successes and five local node resets. This supports the analogous Actions
rule, but the Actions post-rollout controls remain required: canonical discovery
and `/application/o/agentplane-actions/jwks/` success, wrong-SNI and direct
plaintext rejection. Do not change DNS before those controls pass.

## Post-merge results, 2026-09-10 03:54–03:56 UTC

PR #6007 merged as `b11b4a837aef2f984a3123bbde54b588636f5663`.
The live CNP reached generation 10 with the exact Authentik:9000 SNI rule.
Flux applied resources but app health remained failed; do not confuse this
with a missing policy rollout.

From the same Pod network namespace:

- Canonical SNI through `10.106.122.5:443`: discovery and JWKS HTTP 200.
- Wrong SNI, canonical HTTP Host: both endpoints HTTP 403.
- Plain HTTP directly to `10.244.4.24:9000`: connection reset, no HTTP response.
- Five alternating TLS rounds: local public IP reset 5/5; remote public IP
  verified 5/5; Service verified 5/5.

Payload-free veth capture `/tmp/agentplane_service_49261.pcap` at
03:56:21.276553 UTC shows one SYN/SYN-ACK/ACK sequence, then orderly FINs,
with no competing SYN-ACK or reset. The filter excluded all TCP payload at
collection. An earlier capture on port 49260 was empty and is not evidence.

These observations support the Service path and retained SNI enforcement,
not long-term reliability or browser/OAuth acceptance. The app remains
CrashLoopBackOff. No live configuration was manually changed and no new
BuildBuddy invocation was needed for these read-only probes. Prior manifest
validation passed at
<https://app.buildbuddy.io/invocation/55e6491d-2b25-449c-b71f-f4276880138a>.

## Initial app investigation (before #6007 rollout)

Status at the time: prerequisite policy proposal; no DNS change or demonstrated fix.

## Read-only observations, 2026-09-10 03:41–03:45 UTC

Agentplane Pod `agentplane-app-5d6d88b587-g5sff`, IP `10.244.4.80`,
endpoint 290 on `ovh-ns102453`, remains CrashLoopBackOff. Probes used its
existing network namespace via the Cilium agent; no container or cluster
configuration was changed. This is transport diagnosis, not live acceptance.

The generated `gateway-system/cilium-gateway-cluster-gateway` Service is
NodePort with ClusterIP `10.106.122.5`. Cilium service 159 maps port 443 to
`127.0.0.1:80`; its generated Envoy listener supports both raw HTTP and TLS
filter chains. The EndpointSlice's `192.192.192.192:9999` is a placeholder,
not the actual datapath backend.

All probes retained certificate verification and explicit SNI. HTTP probes
requested only public OIDC discovery, with Host `auth.allegedly.works`.
Only status lines and the Service denial's server/body were recorded; no
credentials, cookies, authorization codes, or sessions were used.

| Source / destination                   | Auth SNI TLS     | Discovery HTTP                   |
| -------------------------------------- | ---------------- | -------------------------------- |
| Pod → local node `147.135.37.175:443`  | reset, errno 104 | not attempted                    |
| Pod → remote node `147.135.39.176:443` | verified TLS 1.3 | 200                              |
| Pod → Service `10.106.122.5:443`       | verified TLS 1.3 | 403, server envoy, Access denied |
| Host → Service                         | verified TLS 1.3 | 200                              |

Negative SNI `oidc-policy-negative.allegedly.works` is rejected during TLS
on both node-IP paths but completes verified TLS through the Service. Sending
discovery with that SNI and the canonical Host still returns 403. Thus Service
TLS success is not evidence that the complete request is permitted or that
the SNI restriction is lost. A short endpoint drop monitor showed no drops
during the first Service TLS probe.

## Why a policy change is the next experiment

Cilium revision `9a8982433e18019e290b8199c0c4ad24f66befe8`,
`bpf/bpf_lxc.c`, forwards L7 Service traffic to its proxy before ordinary
egress policy evaluation. Deployed proxy revision
`edeb3f2af56c37c407efa1f63f0b32f595399bbc`,
`cilium/network_filter.cc`, captures the original SNI and checks policy in
the upstream callback against the selected backend identity and port.
`cilium/filter_state_cilium_policy.cc` applies the source Pod's egress policy
using that destination and SNI. This explains why node:443 permission is
insufficient for this path. The precise live deny callback was not traced.

Authentik's Service selects component=server, instance=authentik,
name=authentik, and maps HTTP to 9000. The proposed rule adds only those Pods
in the authentik namespace on TCP 9000 with canonical SNI. It does not grant
unrestricted plaintext backend access. Its runtime enforcement must still
be verified; source inspection does not prove the proposed rule succeeds.

## Post-review validation gate

After GitOps applies the policy, repeat canonical and negative SNI probes
from the app network namespace, including HTTP requests. Require discovery
and JWKS 200 with canonical SNI, rejection with negative SNI, and rejection
of direct plaintext backend access. Repeat the local/remote/Service matrix
with payload-free captures before concluding the Service path is reliable.
Keep public DNS and the issuer unchanged during this experiment.

Only after these checks should internal DNS routing be proposed. Real OIDC
acceptance must use public-coder-devbox and wait for application readiness;
the current app crash loop is a separate blocker. No BuildBuddy validation
invocation exists for these read-only TLS/HTTP probes.
