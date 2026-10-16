# Use credentials through the proxy

Read `GET /v1/rules` on the Egress rules base URL from your platform instructions, authenticated
with `Authorization: Bearer agentplane-credential-agentplane-workload`. Preserve the configured
proxy and CA trust for this call and subsequent protected traffic.

## Build the request from a live rule

Choose a rule covering the intended host, HTTP method, and path. Read the credential's description:
it identifies whose authority you will use. Send its exact `placeholder` according to its `targets`:

- `schemeToken`: use the declared header and scheme, such as `Authorization: Bearer <placeholder>`.
- `wholeValue`: the entire declared header value is the placeholder.
- `basicUsername`, `basicPassword`, or `basicWhole`: put the placeholder in the declared Basic
  authentication component; let your HTTP client encode that component normally.
- `jsonField`: the entire string value of the named top-level JSON field is the placeholder.
  Follow the rule's body constraints; nested fields and arbitrary interpolation are unsupported.

Use the live schema for complete target definitions. A placeholder must occupy the declared
component exactly; a substring or a different presentation will not substitute. Do not redact,
rename, or replace it with `$TOKEN`, `<redacted>`, or a made-up value. Never obtain, print, or
persist the substituted real credential. A literal placeholder is not the real secret.

The placeholder is `agentplane-credential-<name>`, where `<name>` is the EgressCredential's
`metadata.name`, not a PolicySet name. If you already know that exact credential name, its derived
literal is usable after checking the current route and presentation; no PolicySet lookup is needed
just to construct it. The rules response gives the literal directly.

Preserve proxy settings in child tools. A placeholder forwarded to a different machine or remote
execution service may remain inert: that process does not inherit your Sandbox's substitution
path or workload identity. Establish a supported authentication path for that execution environment.

## Diagnose a refusal

Inspect the response status and `x-agentplane-egress` header. An egress denial identifies a reason
such as `no-rule`, `placeholder-unresolved`, `credential-unavailable`, or `unavailable`.
Re-read your effective rules and compare the actual destination, method, path, and presentation.
A downstream service can separately reject authentication or authorization after egress succeeds.

Report a missing route, credential, or unavailable proxy with the intended operation and scope.
Use an independently authorized alternative, or request a narrow temporary PolicySet through the
Actions service's Kubernetes/grants guide. Do not bypass the proxy, disable certificate
verification, reuse another account's credentials, or repeatedly retry an unchanged denied call.
