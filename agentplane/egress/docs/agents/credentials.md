# Use credentials through the proxy

Run `agentplane egress rules` ([HTTP reference](http_api.md) for the raw request and response fields).
Preserve the configured proxy and CA trust for this call and subsequent protected traffic.

## Build the request from a live rule

Choose a rule covering the intended host, HTTP method, and path. Read the credential's description:
it identifies whose authority you will use. Send its exact `placeholder` according to its `targets`:

- `METHOD_SCHEME_TOKEN`: use the declared `header` and `scheme`, such as `Authorization: Bearer <placeholder>`.
- `METHOD_WHOLE_VALUE`: the entire declared `header` value is the placeholder.
- `METHOD_BASIC_USERNAME`, `METHOD_BASIC_PASSWORD`, or `METHOD_BASIC_WHOLE`: put the placeholder in the
  declared Basic authentication component of the `header`; let your HTTP client encode that component
  normally.
- `METHOD_JSON_FIELD`: the entire string value of the top-level JSON `field` is the placeholder.
  Follow the rule's body constraints; nested fields and arbitrary interpolation are unsupported.

A target carries only the fields its method uses, so a field it lacks is absent. A placeholder must
occupy the declared component exactly; a substring or a different presentation will not substitute. Do not redact,
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
