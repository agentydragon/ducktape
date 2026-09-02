# Squid egress-proxy spike

Archived after the spike was retired. Its Flux declarations are suspended
historical records; this directory is not reconciled by the active cluster
configuration.

Throwaway spike deciding whether **one Squid per fence** can replace the
iron-proxy + cache split as Haku's egress fence. Rationale, alternatives and the
earlier 3.5 results: <../../../docs/plans/agent_egress_proxy_options.md>.

**Holds no real credentials.** Every value in `app/fake-credentials.yaml` is
fake, deliberately a ConfigMap rather than a Secret, because the spike proves a
mechanism and a real credential would add risk without adding information.

## Questions it answers

The earlier spike ran ad-hoc on Squid 3.5.27 from 2017 and proved header
substitution works on `ssl_bump`-decrypted requests. These are what it left
open, all encoded in `app/squid.conf` and `app/credentials.conf.tmpl` so they
are answered by running rather than by reading:

1. **Current-Squid port.** The header directives are unchanged since Squid 2.x,
   but the TLS ones moved. Partly a rename (`ssl_crtd` → `security_file_certgen`)
   — but see the findings below, because one of them is not a rename and the
   difference is silent. The image tracks the newest stable series (7.x today,
   via `alpine:3.23`) rather than whatever the base image happens to carry:
   upstream supports only the newest series, and answering these questions on a
   Squid we would not deploy is how a silently-changed directive reaches a
   fence.
2. **Destination scoping.** Every rule is `(placeholder AND destination)`.
   Gating on the placeholder alone is an exfiltration primitive — the agent
   holds the placeholder, so it could redeem it at a host of its choosing.
3. **Several credentials per fence.** Three rules on the same `Authorization`
   header with different placeholders and destinations, which the 3.5 run never
   exercised.
4. **base64 `Basic`.** The git-over-HTTPS shape, matched as a base64 blob in a
   `req_header` ACL.
5. **Caching.** `cache_mem` with no `cache_dir`, plus an explicit
   `cache deny has_auth` so an authenticated response cannot be stored.

## Running it

```bash
kubectl -n squid-egress-spike exec deploy/squid-spike -c squid -- \
  curl -sk --proxy http://127.0.0.1:3128 \
    -H 'Authorization: Bearer spike-bearer-placeholder' \
    https://echo-origin.squid-egress-spike.svc.cluster.local:8443/ | jq .headers
```

The origin echoes the headers it received, so the substituted `Authorization`
and the proxy-stamped `X-Spike-Client` appear directly in the response. Squid's
`debug_options ALL,1 11,2` also logs the literal upstream request to stdout, so
`kubectl logs` is a second, origin-independent view.

Expected, and the point of the exercise:

| Request                                                 | Origin should see                    |
| ------------------------------------------------------- | ------------------------------------ |
| `Bearer spike-bearer-placeholder` to the echo origin    | `Bearer fake-real-bearer-do-not-use` |
| `Bearer spike-bearer-placeholder` to **any other host** | the placeholder, unmodified          |
| `Bearer something-else`                                 | passed through untouched             |
| no `Authorization`                                      | stays absent                         |

The second row is the security property. If a placeholder is substituted at a
destination its rule does not name, the design is wrong, not the config.

## Findings

**Run 1, 2026-08-10 — `ssl_bump` works on 6.12; upstream TLS did not.**

Established:

- **Bumping works.** Squid 6.12 decrypted the client `CONNECT` using the
  cert-manager-issued CA and logged the plaintext `GET /` with its
  `Authorization` header intact. Question 1's hard half is answered.
- **The credential rules render correctly.** `envsubst` produced all three
  destination-scoped rules on tmpfs, each `(placeholder AND destination)`.
- **`DONT_VERIFY_PEER` is a Squid 5 idiom that 6.x silently repurposes.** Every
  bumped request failed `ERR_SECURE_CONNECT_FAIL` while `openssl s_client` from
  the same pod completed the same TLS 1.3 handshake to the same Service IP. The
  error page said `[No Error] (TLS code: [Unknown Error Code])`; only
  `squid -k parse` named it, as a deprecation warning. `sslproxy_cert_error` is
  the 6.x directive, and section 83 debugging is now on so a repeat says so in
  the log.

Not yet answered: substitution, multi-credential, base64 `Basic`, and caching —
all four need a request to reach the origin, so they are blocked behind the
above until the next run.

A general lesson for the fence work: a config that _parses_ is not a config that
_does what it says_. Squid accepted a directive that no longer had the intended
effect and reported no error at request time either.

**Run 1 ran on 6.12; the image has since moved to 7.6.** That was the direct
consequence of the finding above — `alpine:3.22` pinned us to a series upstream
calls archival, and the whole point of the spike is to answer questions about the
Squid a fence would actually run. The bump means run 1's TLS finding is history
and the four open questions get answered on 7.6. Anything below that predates the
bump is labelled with the version it was observed on.

**Run 2, 2026-08-10, Squid 7.6-VCS on `alpine:3.23` — all five questions
answered.** Squid started clean on the new base, so the `/dev/shm` workaround and
the `security_file_certgen` init both survived the OpenSSL/musl move.

Verdict per question: (1) 7.x port — **yes**, with the `DONT_VERIFY_PEER` trap
above; (2) destination scoping — **security property holds**, and the strip
semantics were settled and re-verified below; (3) several credentials — **yes**; (4) base64 `Basic`
— **yes**; (5) caching — `cache deny has_auth` **works**, hit path not
demonstrated.

Observed:

- `Bearer spike-bearer-placeholder` → origin saw
  `Bearer fake-real-bearer-do-not-use`. Substitution works post-bump on 7.6.
- `Basic c3Bpa2U6cGxhY2Vob2xkZXI=` → origin saw
  `Basic c3Bpa2U6ZmFrZS1yZWFsLXBhc3N3b3Jk`. Matching a base64 blob in a
  `req_header` ACL behaves, so the git-over-HTTPS shape is fine.
- `Bearer something-else` passed through untouched, and a request with no
  `Authorization` stayed without one. Two rules on the same header coexisted, each
  matching only its own placeholder.
- **The security property holds.** `Bearer spike-other-placeholder`, whose rule
  names `example.invalid`, was **not** substituted at the echo origin. A
  placeholder is not redeemable at a destination its rule does not name.
- Authenticated responses were not cached (`Cache-Status: …;detail=no-cache`),
  so `cache deny has_auth` does what it says.

### Decided and fixed: a placeholder at an allowed host passes through

Case (b) did not arrive _unchanged_ — the `Authorization` header arrived
**absent**. That is because each rule's two halves are scoped differently:

```squid
request_header_access Authorization deny ph_other                # every destination
request_header_add    Authorization "Bearer …" ph_other to_elsewhere  # one destination
```

The `deny` strips the header wherever the placeholder matches; only the `add` is
destination-scoped. So a placeholder presented to the wrong host is deleted
rather than forwarded.

It **fails safe** — no real credential leaks — but it is a different contract
from the one this design wants, and it was an accident of how the rules were
written rather than a decision.

**Resolved: pass it through.** Substitution is meant to be a swap the caller can
opt out of by sending something else; silently eating an unrelated header is not
that. A placeholder aimed at a host that is otherwise allowed should reach it
unmodified, and a host that is _not_ allowed is already refused by the allowlist,
so nothing rests on the strip. The destination ACL now appears on both lines of
every rule — Squid ANDs the ACLs on a directive line:

```squid
request_header_access Authorization deny ph_other to_elsewhere
request_header_add    Authorization "Bearer …" ph_other to_elsewhere
```

Leaking the placeholder string to an allowed host costs nothing: the agent
already holds it, and redeeming it still requires the destination its rule names.
`credentials.conf.tmpl` carries this as an invariant, since scoping only the
`add` is the easy mistake and it is invisible until someone tests the negative
case.

**Confirmed by re-running on 7.6-VCS**: case (b) now arrives as
`Bearer spike-other-placeholder`, unmodified. Substitution at the named
destination still works for both the `Bearer` and the base64 `Basic` rule, so
scoping the `deny` did not disturb the path it shares with the `add`.

### Two things this run could not show

- **`X-Spike-Client` was `127.0.0.1`.** The test drives curl from inside the
  squid container, so `%>a` correctly reported loopback. The mechanism works; a
  meaningful caller identity needs a request from a second pod.
- **No cache hit was demonstrated.** Both unauthenticated fetches reported
  `fwd=stale`, because the echo origin returns responses with no freshness
  information. Measuring a hit rate needs an origin that sends cacheable
  responses — the `cache deny has_auth` half is the security-relevant one and it
  is confirmed.

## Pull credential

The image is private, and the `forgejo-images-creds` pull secret reaches this
namespace by **reflector mirroring** — `squid-egress-spike` must appear in both
`reflection-allowed-namespaces` and `reflection-auto-namespaces` on
<../../../k8s/forgejo-images/registry-creds.sops.yaml>. That is a grant of one
dockerconfigjson and nothing else, and it is the mechanism
<../../../docs/container-images.md> § Forgejo-hosted images prescribes.

The first cut of this spike instead copied the `ExternalSecret` that
`haku-egress-proxy` uses. That failed closed — `denied by spec.condition` — and
the fix would have been to add this namespace to
`kubernetes-flux-system-secret-store`, which grants read access to _every_
secret in `flux-system` (the GitHub App, four PATs, the Route 53 credentials,
`ci-age-key`). Wrong trade for a directory whose own README opens by saying it
holds no real credentials.

## Ordering when this first lands

The image does not exist until `.github/workflows/squid-ssl-image.yml` runs on
`devel`. Until then the Deployment references a placeholder tag and the
`squid-egress-spike-app` Kustomization stays un-ready; Flux replaces the tag via
the `$imagepolicy` marker once CI publishes, and it self-heals. Same transient
as any first-push image here — see <../../../docs/container-images.md>.

## Disposal

This is a spike, not infrastructure. The active cluster wiring and live objects
are retired; this archive keeps the experiment's findings and suspended Flux
declarations as reference material. Delete the archive together with
`cluster/images/squid-ssl/`, the workflow, and the two
`flux-image-automation-forgejo/squid-ssl-*` files when that reference is no
longer useful.
