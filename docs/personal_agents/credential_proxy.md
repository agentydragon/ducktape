# Credential proxy — decision record

How `public-coder-agent` gets the power of its GitHub token without ever
possessing it. The shipped design is
[iron-proxy](https://github.com/ironsh/iron-proxy) in `replace` mode
(<../../cluster/k8s/agents/public-coder-agent/proxy/iron.yaml>) behind a
NetworkPolicy that makes the proxy the pod's only route out. Measurements: F15,
F16 in [findings/egress_and_tls.md](findings/egress_and_tls.md); F7, F10 in
[findings/credentials.md](findings/credentials.md).

## The shape is forced: MITM forward proxy

The one hard property is possession — the agent must not hold the credential.
Reverse-proxy injectors (every LLM gateway, Envoy's `credential_injector`) only
work when the client's endpoint is a knob (`base_url`); a coding agent runs
`git clone https://github.com/...` and `gh`, where the hostname is baked into
remotes, API defaults, and every README it reads. Injecting for unmodified
tools against real hostnames means terminating TLS inside `CONNECT` tunnels
against a CA the workload trusts — a MITM forward proxy. (Measured with Envoy
on two listeners: the reverse leg injects and returns 200s; the forward leg
sees only an opaque tunnel and GitHub answers 401.)

## Contract facts for readers of `iron.yaml`

- **`replace`, not `inject`, deliberately.** The agent holds
  `GH_PAT=proxy-github-placeholder` and presents it exactly as it would a real
  token; the proxy swaps the value on scoped hosts. `inject` (the proxy always
  attaches; the client sends nothing) hides the credential's existence — an
  agent that goes looking finds none and concludes the task is impossible,
  which is what F7 looks like from the outside. A leaked placeholder is a
  non-event.
- **The placeholder contract is told to the agent, not enforced.** `replace`
  acts only on requests carrying the placeholder; a request without it is an
  unauthenticated request, not a failure. `require: true` cannot enforce it in
  explicit-proxy mode: it is evaluated against the header-less `CONNECT` and
  rejects every HTTPS request with 403 (F15).
- **Rules scoped by method or path block their own `CONNECT` preflight** unless
  each is paired with a `methods: ["CONNECT"]` rule — the host becomes
  unreachable while the config looks correct (F15).
- **Basic auth is why iron-proxy and not our addon.** git-over-HTTPS
  authenticates with `Authorization: Basic base64(user:token)`; iron-proxy
  decodes, substitutes, and re-encodes it — verified with a real 3.9 MB push
  (F16). The 1 MiB `max_request_body_bytes` default does not apply to the git
  transport.
- **A missing Secret fails loud, not open**: the token arrives by
  `secretKeyRef`, so an absent Secret or key stops the pod at
  `CreateContainerConfigError` before it serves a request. Only a
  present-but-empty value degrades to confusing 401s.
- **Request-level path policy is defence in depth, not the boundary.** The fork
  constraint is GitHub's to enforce: `agentydragon-agent` opens PRs from its own
  forks and the token holds no write access to anyone else's repository. The
  stronger move on this axis is narrowing the credential itself (GitHub App
  installation token or fine-grained PAT), which makes scope a reviewable fact
  about the credential rather than a proxy rule that can drift.
- Practices kept regardless of engine: **strip client-supplied auth headers**
  rather than only overwriting `Authorization`; and **`.git/config` is a
  credential store** — `actions/checkout` persists the token there as a base64
  header, so `git config --global credential.helper ""` belongs in the image.

## Rejected options

Kept to rejections a future author might plausibly re-try; the full survey and
candidate table are in git history (`credential_proxy_options.md`, which this
file distills).

One direction is rejected only as today's fence, not as an end state: an HTTP
egress control plane where **haku-console makes the per-request decision** and
a proxy merely enforces it (a mitmproxy-style addon was one candidate engine)
is an in-progress track —
[#4670](https://github.com/agentydragon/ducktape/issues/4670). Its current
contract is <../../haku/egress/SPEC.md>; research and alternatives remain in
<../../cluster/docs/plans/agent_egress_proxy_options.md>.

- **Our mitmproxy addon** — worked and was smaller; lost narrowly because
  iron-proxy expresses the same host+method+path policy as YAML from a
  maintained project and handles git's Basic shape, which the Bearer-only addon
  never did. The accepted cost of the switch: a ~540-star, roughly year-old Go
  binary in the credential path in place of mature mitmproxy.
- **kloak (eBPF, in-kernel swap at `SSL_write`)** — the strongest alternative:
  no proxy in the data path, no CA at all (deletes the F8 failure class), fails
  safe (a missed hook sends the placeholder — an outage, not a leak), and this
  stack lands on its clean exported-symbol path (Node here still exports
  `SSL_write`). Rejected because it couples a security control to TLS-library
  ABI internals — per-version, per-arch byte-offset tables for stripped
  runtimes — and wants a privileged eBPF DaemonSet on every Talos node from a
  young project. HTTP coupling survives unattended dependency bumps; ABI
  coupling does not. The legitimate end state it suggests — kloak for
  possession, the proxy purely for egress — was never trialled.
- **Infisical/agent-vault** — same mechanism, most mindshare, names OpenClaw in
  its README; ruled out operationally: policy state lives in a UI + Postgres
  rather than a git-reviewable file, and every other control here is reconciled
  from git.
- **onecli** — per-agent scoped tokens are a genuine gain the moment there is a
  second agent; costs a gateway + dashboard + store in place of one container.
- **Envoy `credential_injector`** — reverse-only (see above), and marked
  work-in-progress upstream.
- **Squid + `ssl_bump`** — unknown, not ruled out: whether `request_header_add`
  applies to bumped requests is undocumented either way, and the image
  available here (`ubuntu/squid`, gnutls) rejects `ssl-bump`, so it could not
  be tested.
- **OpenShell's supervisor** — the best policy model surveyed: credential use
  scoped to the calling **binary** (only `/usr/bin/git` and `/usr/bin/gh` can
  spend the token), a vantage no proxy has. Unusable until the whole harness
  runs inside it, and OpenShell is ruled out for unattended use
  ([verdicts.md](verdicts.md) § Isolation and sandboxing).
- **`gh-aw-firewall`, CyberArk Secretless, Arcade/Composio/Nango** — wrong
  layer: gh-aw-firewall injects only LLM credentials and leaves `GITHUB_TOKEN`
  in the runner environment (the F7 exposure, unmitigated); Secretless's HTTP
  connector is plain-HTTP-in, not `CONNECT` MITM; the hosted platforms replace
  `git` with their own tool surface.

LiteLLM already is this pattern for LLM traffic — virtual key in, provider key
out, per-key model allowlists as policy — which is why agents get LiteLLM
virtual keys rather than provider keys.
