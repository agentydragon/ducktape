# Matrix password login through Agentplane egress

Iron's public-coder configuration replaces a password placeholder in the JSON body of
`POST https://matrix.allegedly.works/_matrix/client/v3/login`. Agentplane's `jsonField` target supports that exact
substitution without introducing body templates or regexes. This adds the capability only: the existing Iron route and
clients are unchanged, and no Matrix credential, binding or Secret is deployed by this change.

## Configuration for the later cutover

Once credential delivery is configured in the gateway's credentials namespace, these resources in its rules namespace
describe the presentation and the only authorized destination:

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: EgressCredential
metadata:
  name: matrix-login
spec:
  description: Public coder's Matrix bot password, used only for Matrix password login.
  source:
    secretRef:
      name: matrix-bot-password
      key: password
  targets:
    - method: jsonField
      field: password
---
apiVersion: agentplane.allegedly.works/v1alpha1
kind: EgressPolicy
metadata:
  name: public-coder-matrix-login
spec:
  rules:
    - hosts: [matrix.allegedly.works]
      methods: [POST]
      paths: [/_matrix/client/v3/login]
      credentialRef:
        name: matrix-login
```

Bind this policy only to the intended workload identities. The agent discovers the placeholder and the `jsonField`
target from `/v1/rules`, then sends `Content-Type: application/json` and a login object whose `password` value equals
that placeholder. The gateway replaces that value only after workload authentication and host/method/path/credential
authorization. The same placeholder on another otherwise-allowed route is refused, not redeemed or forwarded. The usual
query-insensitive path matching applies; this adds no alternate path normalization.

Deploy the new proxy code and schema before creating credentials with the new target method. An old proxy cannot parse
that method and fails closed. Remove the new grants/credentials before rolling the code back.

## Target variants

Resource parsing, the agent rules API, the operator API and generated TypeScript share one `Target` union discriminated
by `method`. Each variant has only its own required fields:

- `wholeValue`, `basicUsername`, `basicPassword`, `basicWhole`: `method` and `header`.
- `schemeToken`: `method`, `header` and `scheme`.
- `jsonField`: `method` and `field`.

There is no target with nullable `header`/`field`/`scheme` siblings. Mixed variants, missing required fields and unknown
discriminator values are rejected, not normalized or silently pruned by the runtime/API models. Kubernetes admission
enforces the corresponding structural `oneOf`.

## Exactness and limits

- Only one declared **top-level JSON field name** per target; no JSONPath, nested traversal, regexes, string
  interpolation or arbitrary substring replacements.
- The entire field value must equal the placeholder and be a string. Other fields and values retain their JSON meaning.
  Formatting/escaping may change because the object is serialized.
- The replacement is JSON-encoded, including quotes, backslashes, control characters and Unicode. Request framing is
  updated by mitmproxy. No real password appears in the response, rules view or decision evidence as a result of this
  feature; an upstream service's own response behavior remains the upstream's responsibility.
- Body-target routes require an uncompressed UTF-8 `application/json` object of at most 64 KiB. Duplicate keys (at any
  depth), malformed JSON, non-finite numbers, unsupported media/encoding and oversized input are refused with
  `invalid-body`, unless the credential is already presented via a declared header target. This is a substitution
  processing limit, not a new global streaming/upload limit for the proxy.
- Unrelated traffic and header-only targets retain their existing body handling. Bodies outside this format cannot cause
  JSON substitution. Undeclared fields and partial placeholder strings remain inert; the proxy never inserts the
  password there.
- JSON source objects and replacement bytes are not included in object reprs or decision logs. The decision records only
  whether substitution occurred.

The Matrix login response can still contain a Matrix session/access token, as it does with the existing Iron login. This
change hides the login password; it does not turn Matrix sessions into proxy-only credentials or implement response
rewriting.
