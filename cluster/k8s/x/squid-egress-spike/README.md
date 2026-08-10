# Squid egress-proxy spike

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

1. **Squid 6.x port.** The header directives are unchanged since Squid 2.x, but
   the TLS ones were renamed (`sslproxy_flags` → `tls_outgoing_options flags=`,
   `ssl_crtd` → `security_file_certgen`). This config is the 6.x form.
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
| `Bearer spike-bearer-placeholder` to **any other host** | placeholder **not** substituted      |
| `Bearer something-else`                                 | passed through untouched             |
| no `Authorization`                                      | stays absent                         |

The second row is the security property. If a placeholder is substituted at a
destination its rule does not name, the design is wrong, not the config.

## Pull credential

The image is private, and the `forgejo-images-creds` pull secret reaches this
namespace by **reflector mirroring** — `squid-egress-spike` must appear in both
`reflection-allowed-namespaces` and `reflection-auto-namespaces` on
<../../forgejo-images/registry-creds.sops.yaml>. That is a grant of one
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

This is a spike, not infrastructure. When it has answered its questions, delete
the whole directory, `cluster/images/squid-ssl/`, the workflow, the two
`flux-image-automation-forgejo/squid-ssl-*` files, and the root kustomization
entries. The `CLEANUP` markers in those files say the same.
