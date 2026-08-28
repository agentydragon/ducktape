# Substitution smoke test

The deployed console config carries an inert, always-on substitution pair so the fence's
credential-substitution path (#4670, #4951) is drivable end to end with no real credential
anywhere:

- `egress_decide.credentials` entry `substitution-smoke` — placeholder
  `EXAMPLE-EGRESS-SMOKE-PLACEHOLDER`, scanned header `authorization`, redeemable only by the haku
  Agent at `https://postman-echo.com:443`;
- `egress_decide.standing_policies` entry `substitution-smoke` — the reachability half: exactly
  `GET` `/headers` at that origin for the haku Agent, naming the credential
  (`cluster/k8s/haku/console/config.yaml`);
- the "credential" value — the committed literal `EXAMPLE-EGRESS-SMOKE-SUBSTITUTED`
  (`HAKU_EGRESS_CREDENTIAL_SUBSTITUTION_SMOKE` in `cluster/k8s/haku/console/deployment.yaml`).
  Deviation from the Secret-backed registry slots, deliberately: both sides of this swap are inert,
  which is the point — the swap is observable in cleartext at the echo host. Identity secrets
  (proxy token, fence credentials) stay Secret-backed and fail-loud.

`postman-echo.com/headers` reflects request headers back as JSON, anonymously and statelessly.
Chosen over `httpbin.org` (same API shape) for uptime: Postman operates it as a documented product
service, while httpbin.org is community-hosted with a history of outages.

## Watch the swap

From any pod in `haku-sandbox` (a warm `haku` sandbox via the console's `sandbox.exec_sandbox`, or
`kubectl exec` into a pod labeled `app.kubernetes.io/name=haku-sandbox`):

```bash
curl -sS -x http://haku-egress-proxy.haku-console.svc.cluster.local:8888 \
  -H 'Authorization: Bearer EXAMPLE-EGRESS-SMOKE-PLACEHOLDER' \
  https://postman-echo.com/headers
```

`-x` targets the colocated fence directly: the Kyverno-injected `HTTPS_PROXY` still points at the
port-8080 iron fence until the #4943 cutover, but the force-proxy CCNP already admits
`haku-console:8888`. No `--cacert` needed — the injected `CURL_CA_BUNDLE` trusts the shared
interception CA whose leaves the colocated proxy serves.

The reflected JSON proves the swap happened between the pod and the upstream:

```json
{ "headers": { "authorization": "Bearer EXAMPLE-EGRESS-SMOKE-SUBSTITUTED" } }
```

Decision provenance is in the sidecar log
(`kubectl logs -n haku-console deploy/haku-console -c egress-proxy`):

```text
allow GET postman-echo.com:443 -> <ip> decision_id=standing:substitution-smoke (substitutions: 1 of 1 applied)
```

Placeholders and values never appear in proxy logs (#4670).

## What a non-granted request yields

The standing entry admits the `CONNECT` to `postman-echo.com:443` by origin match alone; the
method/path pin binds each decrypted inner request. So where a request is refused depends on what
missed:

- **Covered origin, uncovered request** — `GET /get`, `POST /headers`, or `/headers?x=1` (the path
  pin is a fullmatch over path plus query): the tunnel opens, the inner request comes back
  HTTP 403 `egress denied: no active HTTP grant covers the request`.
- **Uncovered origin** — e.g. `https://example.com/`: the `CONNECT` itself is refused; curl fails
  with a 403-from-proxy error, and the sidecar logs
  `deny CONNECT example.com:443: no active HTTP grant covers the origin`.
- **Granted request without the placeholder**: forwarded untouched — the placeholder is the
  capability handle; the echo reflects exactly the header you sent.
- **Placeholder in an unscanned header** (anything but `authorization`): rides through verbatim.
- **Value env var unset** (the deployment literal removed): the console skips the credential at
  startup with a warning (#4970); the standing entry still admits, and the placeholder arrives at
  the echo host unsubstituted.

## Scope and retirement

haku-Agent-only, one method, one path, one public origin. A temporary grant
(`http_grants.create_grant` naming `credential_handle`) redeems the same registry, but the deployed
config does not expose the `http_grants` server yet — the standing entry is the deployed path.
Retire the drill by deleting the credential entry, the standing entry, and the deployment env line
together.
