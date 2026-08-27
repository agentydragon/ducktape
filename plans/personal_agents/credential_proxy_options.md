# Credential-injecting proxies: what exists, and what fits

Question: we built a mitmproxy addon that holds the GitHub token so the agent
never possesses it ([lab_notes.md](lab_notes.md) F10). Is there an off-the-shelf
thing that does that?

**Scope note, because an earlier draft of this document got it wrong.** The addon
also carries a host+method+path rule confining writes to the agent's own fork,
and that rule was treated here as a requirement to evaluate candidates against.
It is not one. `agentydragon-agent` has its own GitHub account and opens PRs from
its own forks, so **GitHub already enforces the fork constraint** — the token
holds no write access to anyone else's repository, and a proxy path rule is a
second copy of a boundary the forge owns. Request-level policy is a **bonus**:
useful for defence in depth and for narrowing what a stolen-in-flight credential
can do, never the thing that makes the fork constraint true. The only hard
property is the one in the question — the agent must not possess the credential.

Answer: **yes — there is a whole 2026 ecosystem for exactly this.** "Give the
agent the power of a token without giving it the token" is a well-populated
problem space, and our hand-written addon is one point on a frontier rather than
the only option.

The landscape falls into three camps, and which one a tool belongs to decides
whether it can serve a coding agent at all.

**Search note, because it determined the answer — and because the first two
passes got it wrong.** Searching for "credential injection proxy" and for named
enterprise products finds only camp 1. The ecosystem organises itself around
_the agent_, not around the proxy, so the productive queries are about **agent
sandboxes** and **agent runtime security**; one curated list
([awesome-agent-runtime-security](https://github.com/bureado/awesome-agent-runtime-security))
covers most of the field in a single table.

Finding that list was not enough. Its "Secrets Management & Credential
Injection" section has ~19 entries and the first survey here carried three of
them, because the list was cited as a source rather than read row by row —
`iron-proxy` appears in the camp 2 paragraph below and went straight past
without being followed up, and the two strongest candidates
([iron-proxy](https://github.com/ironsh/iron-proxy) and
[Infisical/agent-vault](https://github.com/Infisical/agent-vault), 540 and ~2k
stars) were both missed. The queries that did work, kept because they are
reusable: the artifact plus its mechanism (_"proxy replaces placeholder API key
with real credential"_), the problem in the user's own words (_"prevent token
exfiltration from an agent"_), and `Show HN` — every project in this space
announced there, which makes it a better index than any search engine.

Two habits follow. **Enumerate a curated list, do not cite it.** And **treat a
name you cannot immediately place as a work item**, because that is exactly how
the best option here was skipped twice.

## Camp 1: reverse-proxy injectors — the split is about who chooses the URL

Every credential injector in production today is **reverse-proxy shaped**. The
client is pointed at the proxy (`base_url`, `OPENAI_BASE_URL`, a sidecar on
localhost), the proxy knows the single upstream it fronts, and it attaches the
credential on the way out.

That works when the client's endpoint is configurable. It is why every LLM
gateway is built this way: an SDK takes a `base_url`, so redirecting it costs one
environment variable.

It does not work for a coding agent, because the agent runs `git clone
https://github.com/owner/repo` and `gh pr create`. The hostname is not a knob —
it is baked into remotes, into `gh`'s API host, into every URL in every README it
reads. Serving those from a reverse proxy means rewriting URLs everywhere and
fighting GitHub's own absolute redirects.

## Camp 2: MITM forward proxies

Making injection work for **unmodified tools against real hostnames** requires
terminating TLS for arbitrary hosts inside a `CONNECT` tunnel, which means minting
certificates on the fly against a CA the workload trusts. mitmproxy and Squid with
`ssl_bump` both do that.

Our addon lives here, and this is where the whole off-the-shelf field turned out
to be: [iron-proxy](https://github.com/ironsh/iron-proxy),
[Infisical/agent-vault](https://github.com/Infisical/agent-vault),
[onecli](https://github.com/onecli/onecli),
[agentcage](https://github.com/agentcage/agentcage) (MIT, placeholder secrets with
**inbound redaction** as well as outbound substitution), `airut`'s masked
secrets, and `agent-creds` (Envoy made to MITM via iptables plus macaroon
tokens). The first three are container-deployable; the rest are CLI/Podman/Lima
shaped, and agentcage self-describes as experimental and unaudited.

**Squid is unknown, not ruled out.** `request_header_add` is a built-in
directive, but whether it applies to `ssl_bump`-decrypted requests is undocumented
either way in the official reference, and the Squid image available here
(`ubuntu/squid`, gnutls) rejects `ssl-bump` outright — so it could not be tested.

## Camp 3: kernel-space injection — no interception at all

The camp I missed, and the one that changes the calculus.
[kloak](https://github.com/spinningfactory/kloak) (AGPL-3.0, Kubernetes-native)
attaches eBPF uprobes to the TLS write path and swaps a placeholder for the real
secret **in-kernel, immediately before encryption**. There is no proxy in the data
path, no CA to distribute, and no certificate to drift — which deletes the entire
F8 failure class rather than mitigating it.

Mechanically: a controller watches Secrets labelled `getkloak.io/enabled=true`,
creates a shadow Secret whose value is a length-matched `kl::<UUID>` placeholder,
and a mutating webhook rewrites pods to mount the shadow. Real values live only in
eBPF maps. Hooks cover `SSL_write`/`SSL_write_ex` (OpenSSL, and **BoringSSL even
though Node.js links it statically**) and Go's `crypto/tls.(*Conn).Write`.
Destination policy is per-secret annotations `getkloak.io/hosts` and
`getkloak.io/port`, verified by matching TCP destinations against DNS responses
captured in-kernel — so the secret cannot be redirected to an attacker's IP.

That covers our stack on paper: OpenClaw is Node, `git`/`curl` use OpenSSL,
kernels here are 6.18 against a 5.17+ floor.

`Riptides` occupies the same camp with SPIFFE workload identity and
Vault/OpenBao-sourced credentials.

### The test that establishes it

Same Envoy, same credential, same `credential_injector` filter, two listeners:

```text
reverse proxy (client points at Envoy, upstream fixed to api.github.com)
  GET /user                              -> 200  "login": "agentydragon-agent"
  GET /repos/agentydragon/ducktape       -> 200
  PATCH /repos/agentydragon/ducktape     -> 403  (RBAC filter denies)
  POST /repos/agentydragon-agent/.../refs-> 422  (allowed; GitHub rejects empty body)

forward proxy (client keeps real URLs, HTTPS via CONNECT)
  curl -x envoy:8081 https://api.github.com/user
                                         -> 401  "Requires authentication"
```

The 401 is the whole answer. Envoy sees an opaque TLS tunnel and has no
dynamic-certificate machinery, so the filter never gets a request to act on. The
reverse-proxy leg proves the credential and filter were configured correctly, so
the forward-proxy failure is structural rather than a misconfiguration.

### Source review: how well does kernel injection actually generalise?

Read the source rather than installing it. The short version: **the symbol path is
principled, the stripped-binary path is ABI archaeology**, and which one you land
on depends on your runtime.

Four TLS families are handled, not the whole world: OpenSSL and GnuTLS by
**exported symbol name** (`SSL_write`/`SSL_write_ex`,
`gnutls_record_send`/`_send2`), Go by `crypto/tls.(*Conn).Write`, and BoringSSL by
walking internal struct offsets. Nothing covers rustls, JSSE, SChannel, or wolfSSL.

Where symbols exist this is clean — resolve, attach, done. Where they do not,
kloak carries **hardcoded per-version, per-architecture byte offsets**: 12 entries
for Bun (`"1.3.14/amd64": {SSLWriteOffset: 63314768, ...}`, with comments like
"discovered from bun-v1.3.12 profile build") and 7 for Go. Every new upstream
release needs a new table entry.

The BoringSSL path goes further, walking
`SSL* → SSL3_STATE* → SSLAEADContext* → AES_KEY.rd_key` to recover the AES-GCM
round-key schedule, with a source comment explaining that BoringSSL keeps only the
precomputed GHASH powers table and discards the raw subkey. That is needed because
there are **two** mechanisms, not one: a uprobe that rewrites the plaintext buffer
before encryption, and a TC egress path that patches already-encrypted packets —
which requires the session key. The second is dramatically more invasive than
"hook `SSL_write`" suggests.

**For our stack specifically it lands on the clean path**, measured rather than
assumed. OpenClaw's Node v24.16.0 statically links OpenSSL and still exports the
symbols:

```text
ldd /usr/local/bin/node | grep ssl   -> nothing (statically linked)
.dynsym  -> ['SSL_write', 'SSL_write_ex']
.symtab  -> ['SSL_write', 'SSL_write_ex']
```

So no offset table would be involved for us; `git`/`curl`/`python` all use dynamic
OpenSSL and resolve the same way.

**The failure mode is safe, which is what makes the fragility tolerable.** The pod
only ever mounts the shadow Secret, so if a hook does not fire the placeholder
goes on the wire and the upstream returns 401. A runtime upgrade that moves the
symbols causes an **outage, not a leak** — the real secret cannot escape, because
userspace never has it.

**But the strategic point cuts the other way.** Kernel injection couples a
security control to _library ABI internals_, which change without notice and with
no compatibility promise. A MITM proxy couples to _HTTP_, which does not change.
Ugly and protocol-level beats elegant and ABI-level for something that must keep
working through unattended dependency bumps. That is the argument for staying
where we are, and it is stronger than the CA-management argument for moving.

### How NemoClaw runs a harness inside a sandbox without an `entrypoint` field

**The harness runs inside the sandbox.** Worth stating plainly, because the
documentation reads ambiguously: "NemoClaw forwards arguments to the
manifest-declared interactive command via `openshell sandbox exec`" describes how
a turn is _driven_ non-interactively into a harness that is already running there
— not the harness living outside with turns exec'd in. `nemoclaw <name> logs`
reads that harness's logs from inside the same sandbox.

The mechanism that starts it is the **image manifest**: a sandbox image declares
its runtime/interactive command, and the supervisor runs it. That is why no
`entrypoint` field is needed — the command is baked into the image and declared,
rather than set per-Sandbox. So "bake an image that starts the harness" is not a
workaround; it is the supported path, and it is what NVIDIA ships in
`sandboxes/openclaw`.

**Inference is where the credential model shows up best.** The agent inside the
sandbox always targets `https://inference.local` and never contacts an upstream
provider directly. OpenShell intercepts that traffic and injects the stored
credential, so the sandbox never receives an API key. Same placeholder discipline
as the GitHub provider, applied to the model route — one managed egress with the
credential attached outside the blast radius.

So NemoClaw **does** satisfy the S5 shape: harness inside, credentials outside.
Our earlier `oc-lab` split — OpenClaw's gateway outside, sandbox used only as an
exec backend — was our own topology choice, not a limitation NVIDIA also lives
with.

**What still blocks us is narrower than "no entrypoint", and it is ingress.**
NemoClaw is a host/CLI product: you reach the agent through `connect`/`exec`, and
credentials "stay on the host". We need a long-lived gateway answering the
Authentik outpost over HTTP on 18789, and the Kubernetes operator gives a sandbox
no way to be addressed:

```text
sandbox container ports         -> none declared
Services in openshell-sandboxes -> none
ingress NetworkPolicy           -> only from the openshell gateway pod, TCP/2222
```

**Open question, and the right next experiment if we want S5:** does the k8s
operator honour a manifest-declared long-running command the way the CLI does, and
can anything reach it? `OpenShellSandbox.spec.image` lets us supply our own image,
so the first half is testable. The second half — HTTP ingress to a sandbox — has no
field for it today, and that, not the entrypoint, is the thing to watch.

## Candidates

| Option                                                                | Shape                                                | Injects                                                                                                                                          | Policy                                             | Verdict for us                                                                                              |
| --------------------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Envoy `credential_injector`**                                       | reverse                                              | Bearer/Basic, generic-from-SDS or OAuth2 client-credentials                                                                                      | none itself; compose the `rbac` filter             | **Tested, works, wrong shape.** Also **marked work-in-progress** upstream                                   |
| **`github/gh-aw-firewall`**                                           | Squid (domains) + Node reverse-proxy sidecar (creds) | LLM providers only                                                                                                                               | domain allowlist via Squid                         | Closest prior art, and **it does not inject GitHub credentials**                                            |
| **Modal `credential-injection`**                                      | reverse (Caddy `header_up`)                          | any header                                                                                                                                       | none                                               | A recipe, not a component; same shape limit                                                                 |
| **CyberArk Secretless Broker**                                        | local connector                                      | HTTP Basic, Conjur, AWS SigV4; also SSH/pg/mysql                                                                                                 | none                                               | Maintained (1.7.x) and the most mature, but the HTTP connector is plain-HTTP-in, not `CONNECT` MITM         |
| **Arcade / Composio / Nango**                                         | hosted tool runtime                                  | OAuth per provider, injected at call time                                                                                                        | per-tool                                           | Real products for this problem, but they replace `git` with _their_ tool surface                            |
| **LiteLLM (we already run it)**                                       | reverse                                              | provider keys behind virtual keys                                                                                                                | per-key model allowlist                            | **Already doing this for LLM traffic** and doing it well                                                    |
| **mitmproxy + addon (ours)**                                          | forward, MITM                                        | anything                                                                                                                                         | host + method + path                               | Works today; the CA is its weak point (F8)                                                                  |
| **[iron-proxy](https://github.com/ironsh/iron-proxy)**                | forward, MITM, explicit-proxy or DNS                 | **inject** (client sends nothing) or **replace** (placeholder); header or query param, Go-template format; env/AWS/1Password/GCP-minting sources | host + method + path, default-deny                 | **Tested here and the leading candidate.** Does everything our addon does, as YAML — F15, F16               |
| **[Infisical/agent-vault](https://github.com/Infisical/agent-vault)** | forward, MITM                                        | placeholders (`__github_pat__`), or whole-header replacement                                                                                     | host-level service rules                           | Most mindshare, names OpenClaw explicitly; state lives in a DB + UI, not in git                             |
| **[onecli](https://github.com/onecli/onecli)**                        | forward, MITM                                        | placeholder keys, swapped at request time                                                                                                        | host + path patterns, per-agent scoped tokens      | Right camp; gateway + dashboard + store to operate, and no method-level policy documented                   |
| **[pipelock](https://github.com/luckyPipewrench/pipelock)**           | forward, content-scanning                            | **none** — does not inject                                                                                                                       | exfiltration/SSRF/injection scanning               | Complementary, not a substitute: inspects what our proxy would carry                                        |
| **kloak**                                                             | none — eBPF at `SSL_write`                           | any secret, in-kernel                                                                                                                            | host + port, DNS-verified                          | **Strongest contender.** K8s-native, no CA; needs privileged DaemonSet, no path policy                      |
| **agentcage**                                                         | forward, MITM                                        | placeholders + inbound redaction                                                                                                                 | domain allowlist                                   | Right idea, CLI/Podman/Lima only, experimental and unaudited                                                |
| **Riptides**                                                          | kernel-space                                         | Vault/OpenBao via SPIFFE                                                                                                                         | workload identity                                  | Same camp as kloak; not evaluated                                                                           |
| **Squid + `ssl_bump`**                                                | forward, MITM                                        | `request_header_add`                                                                                                                             | ACLs                                               | **Unknown** — could not test; the available image has no `ssl-bump`                                         |
| **OpenShell (ours)**                                                  | own supervisor as sandbox PID 1                      | `openshell:resolve:env:*` placeholders, fail-closed                                                                                              | **endpoint + binary**, bundled with the credential | **Best policy model here.** Blocked only on wrapping the whole harness — the supervisor owns the entrypoint |

Two things worth pulling out of that table.

**We already run a credential-injecting proxy and it is LiteLLM.** A virtual key
goes in, a real provider key goes out, the consumer never holds the upstream
credential, and per-key model allowlists are the policy layer. It is the same
pattern, in the camp where the pattern works, and it is the reason routing
embeddings through LiteLLM was the right call rather than handing an agent a
direct OpenAI Platform key.

**GitHub's own agentic firewall does not solve our problem.** `gh-aw-firewall` is
Squid for domain enforcement plus a Node sidecar that injects **LLM** credentials
(OpenAI, Anthropic, Gemini, Bedrock, Azure) by having agents point their SDKs at
it. GitHub credentials in that world are just `GITHUB_TOKEN` in the runner's
environment — the exposure F7 describes, unmitigated. Two details worth stealing:
it **strips** client-supplied `Authorization`/`x-api-key`/`Proxy-Authorization`
rather than only setting them, and it deliberately does no request logging.

### iron-proxy — tested end to end, and now a close call rather than a clear win

[iron-proxy](https://github.com/ironsh/iron-proxy) (Apache-2.0, Go, one 20 MB
image) is a camp-2 MITM egress proxy whose YAML expresses, as configuration,
everything our addon expresses in Python. It was run rather than read about,
twice: **F15** against a header-echo endpoint, then **F16** carrying a real
OpenClaw deployment that opened a pull request end to end behind it. Both are in
[findings/egress_and_tls.md](findings/egress_and_tls.md).

Ordered by what actually decides this — possession first, request policy last:

|                                  | ours                                   | iron-proxy                                  |
| -------------------------------- | -------------------------------------- | ------------------------------------------- |
| Agent can read the credential    | no                                     | no                                          |
| Works with unmodified `git`/`gh` | REST API only (Bearer)                 | **Bearer and Basic** — real `push` verified |
| Strips client-supplied auth      | yes                                    | **no** — an invented header rides along     |
| Fail closed on a missing secret  | yes                                    | **not in explicit-proxy mode**              |
| CA                               | cert-manager, planted by initContainer | cert-manager, mounted (`tls.ca_cert`)       |
| Deployment                       | ~40 lines we maintain                  | image + ConfigMap                           |
| _Bonus:_ request-level policy    | host + method + path, in Python        | host + method + path, **in YAML**           |

The Basic-auth row is the one that changes the decision: `git` over HTTPS
authenticates with `Authorization: Basic base64(user:token)`, and iron-proxy
decodes, substitutes and re-encodes it. Our addon only ever writes a `Bearer`
header, so it covers `gh` and the REST API and not the git transport.

Of those two "no" rows only one matters. **Stripping is out of scope**: an agent
that sends its own token already had it, and the property we need is that it
never holds ours. **Fail-open matters less than I first wrote**: it is a property of
`replace` mode, and `inject` mode — where the client sends nothing and the proxy
always attaches the credential, which is our actual model — has no placeholder
that can go missing. That correction came from reading the configuration
reference properly; the tests had used only `replace`.

The larger objection is not in the table: adopting it puts a ~540-star,
roughly year-old Go binary from a vendor with a commercial product into the
credential path, in place of mitmproxy, which is mature and already runs here.
The ~40 lines are ours either way; the engine underneath is what changes.

### The two alternates, and why neither displaces it

**[Infisical/agent-vault](https://github.com/Infisical/agent-vault)** (MIT, Go,
~2k stars) has the most mindshare in this space and names OpenClaw in its own
README. Same mechanism — CONNECT MITM, placeholders like `__github_pat__`, a
generated CA — and host-level service rules, which is enough, since the fork
constraint is GitHub's to enforce. What rules it out here is operational, not
technical: its state lives in a UI and a Postgres database rather than a file,
and every other control in this cluster is reconciled from git. A credential
policy nobody can review in a diff is the wrong piece to make an exception for.

**[onecli](https://github.com/onecli/onecli)** (Apache-2.0, Rust gateway plus a
TypeScript dashboard) offers per-agent scoped tokens and an AES-256-GCM store,
which is a genuine gain the moment there is a second agent. Its host+path
matching is likewise sufficient. The cost is the shape: a gateway plus a
dashboard plus a store to operate in place of one container.

Both stay on the list, and on the hard property — the agent never holds the
credential — all three are equivalent. iron-proxy wins on operational fit, not
on policy expressiveness.

## Which substitution mode, and why `replace`

iron-proxy offers both, and the choice is a design decision rather than a
technical one:

- **`inject`** — the proxy always attaches the credential; the client sends
  nothing. Target is a `header` (emitted with the casing configured) or a
  `query_param`; the value comes from a Go template over `.Value` with a
  variadic `base64` helper, so any wire format is expressible:
  `Bearer {{ .Value }}`, `token {{ .Value }}`, or the documented GitHub shape
  `Basic {{ base64 "x-credential:" .Value }}`.
- **`replace`** — the client holds a placeholder and the proxy swaps it, scanning
  `match_headers`, and optionally `match_path`/`match_query` for APIs that carry
  the token in the URL.

Sources are orthogonal to both: env, AWS Secrets Manager with background
refresh, AWS SSM, 1Password service account, 1Password Connect, keyfile, and GCP
JWT/ID-token **minting**.

**Choose `replace`, for legibility.** `inject` hides the existence of the
credential; `replace` hides only its value. An agent holding
`GH_PAT=proxy-github-placeholder` has an accurate model of its own situation —
there is a credential, it is mediated, and it can be used without being read —
and reaches for it exactly as it would reach for a real token. Under `inject` an
agent that goes looking for a credential finds none, and the reasonable
conclusion is that the task is impossible. F7 is what that looks like from the
outside: an agent authenticating as nobody and unable to explain its own 401s.

Two smaller things fall out the same way. A leaked placeholder is a non-event —
and the real token's prefix leaked once already — so the failure mode this
invites is harmless. And `replace` is the mode measured in F15 and F16, so it is
also the configuration that has already opened a pull request end to end.

**The cost is a contract to state rather than a gap to guard.** `replace` only
acts on requests that carry the placeholder, so the agent has to be told: reach
GitHub with `$GH_PAT`, which is a proxy placeholder whose real value is attached
on the way out. A request without it is not a failure — it is an agent that did
not ask to be authenticated.

Earlier drafts called this "fail-open" and treated it as a defect, conflating two
different things. One is the agent not presenting the placeholder, which
`require: true` addresses (it rejects requests to a matching host that lack the
proxy token, "preventing workloads from bypassing the secret-swap mechanism with
alternative credentials") and which is unusable here because it is evaluated
against the header-less `CONNECT` (F15). The other is the proxy's own secret
being absent — and that one barely exists, because the token arrives by
`secretKeyRef`: a missing Secret or key stops the pod at
`CreateContainerConfigError` before it serves a request. Only a present-but-empty
value would surface as a 401.

## What to do

**The proxy is not optional, whatever holds the credential.** S4 requires
domain-level egress confinement, and a MITM forward proxy is how we get it. So
the real question is narrow: does that proxy _also_ carry the GitHub credential,
or does something else? Every option below assumes the proxy stays.

**iron-proxy is a genuine option and not an obvious upgrade.** It is tested to
the same depth as what we run — real OpenClaw, real PR, domain confinement
enforced from the agent's own vantage (F16) — and it covers the git transport
ours has never exercised. Against that it fails open on a missing secret, and it
swaps a mature engine for a young one in the credential path. Our addon works,
is smaller, and its engine is better established. Reach for iron-proxy when the
addon becomes a maintenance burden; pin by digest and mirror to Harbor if so.

**kloak got stronger once the path policy stopped counting against it.**

|                          | mitmproxy + addon (ours)                 | kloak                                        |
| ------------------------ | ---------------------------------------- | -------------------------------------------- |
| Agent can read the token | no                                       | no                                           |
| Data path                | proxy terminates and re-encrypts TLS     | none — swap happens in-kernel pre-encryption |
| CA management            | required; already caused one outage (F8) | **none**                                     |
| Privilege                | an ordinary pod                          | **privileged eBPF DaemonSet on every node**  |
| Maturity                 | ~40 lines we own                         | 244 stars, ~268 commits, security-critical   |
| _Bonus:_ request policy  | host + method + path                     | host + port only, DNS-verified               |

An earlier draft counted "loses the path/method policy" as a decisive strike
against kloak. It is not: `getkloak.io/hosts` restricts where the credential can
be sent, GitHub restricts what it can do, and between them the fork constraint
holds without any request-level rule. What remains against kloak is real but
narrower — it binds a security control to TLS-library ABI internals rather than
to HTTP, so it needs care across every runtime upgrade, and it wants privileged
kernel access on every node, on Talos, for a young project.

**The recommendation is still the proxy**, on the ABI-coupling and blast-radius
arguments alone. But the eventual shape is legitimately **kloak for possession,
the proxy for egress**: the proxy stops holding a credential at all and becomes
purely the domain allowlist S4 already requires, while the secret never exists
in userspace anywhere.

Worth trialling in the lab before believing any of it, in this order:

1. Does the Node/BoringSSL uprobe actually fire for OpenClaw's binary? This is the
   one that decides everything and is pure fact.
2. Does `git push` (libcurl/OpenSSL) get the swap too?
3. What does a privileged eBPF DaemonSet cost on Talos?

Independent of which wins, keep these:

- **Strip inbound auth headers**, don't just overwrite. Ours sets `Authorization`,
  so a client-supplied `x-api-key` or `Proxy-Authorization` rides along untouched.
  (Applied.)
- **Fail closed when the credential is missing** rather than forwarding
  unauthenticated, which turns a broken secret mount into confusing 401s from
  GitHub instead of one loud local error. (Applied.)
- **Scope credential use to the calling binary**, which OpenShell does and neither
  we nor kloak can. Under our policy an injected prompt that runs `curl` still
  spends the token; under OpenShell's, only `/usr/bin/git` and `/usr/bin/gh` can.
  mitmproxy cannot see the peer executable, so approximating this needs the
  supervisor's vantage point — the strongest single reason to revisit OpenShell
  once the harness can run inside it.
- **Redact inbound too.** agentcage substitutes outbound _and_ redacts secrets in
  responses; we do neither on the return path.
- **Move the addon out of the ConfigMap.** `STYLE.md` forbids code blobs over ~5
  lines in YAML.
- **`.git/config` is a credential store.** `actions/checkout` writes the token as a
  base64 `AUTHORIZATION` header there. Any injection scheme is undone if a helper
  persists credentials to disk; `git config --global credential.helper ""` belongs
  in the image.

**Narrow the credential itself — this is where the write constraint belongs.**
Not because a proxy cannot express a path rule, but because the forge is the
party that actually owns the boundary, and a rule at the proxy is a second copy
that can drift from it. A **GitHub App installation token** is scoped by GitHub
to specific repositories and permissions and expires in an hour; a fine-grained
PAT gets most of the way for less work. Either makes "what can this agent write
to" a fact about the credential rather than a property of a policy file, and a
proxy bug then degrades to "the token's own permissions" instead of "everything
the PAT can reach". Installation tokens also retire the rotation item, since they
rotate themselves.

## Sources

- [Envoy credential injector filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/credential_injector_filter)
- [github/gh-aw-firewall — API proxy sidecar](https://github.com/github/gh-aw-firewall/blob/main/docs/api-proxy-sidecar.md)
- [modal-labs/credential-injection](https://github.com/modal-labs/credential-injection)
- [cyberark/secretless-broker](https://github.com/cyberark/secretless-broker)
- [Arcade.dev on agent integration platforms](https://www.arcade.dev/blog/ai-agent-integration-platforms/)
- [Nango on open-source agent integration platforms](https://nango.dev/blog/best-open-source-api-integration-platforms-for-ai-agents/)
- [bureado/awesome-agent-runtime-security](https://github.com/bureado/awesome-agent-runtime-security) — the list that should have been the first search, and then read row by row
- [ironsh/iron-proxy](https://github.com/ironsh/iron-proxy) and its [configuration reference](https://docs.iron.sh/reference/configuration)
- [Infisical/agent-vault](https://github.com/Infisical/agent-vault) and [docs.agent-vault.dev](https://docs.agent-vault.dev/)
- [onecli/onecli](https://github.com/onecli/onecli)
- [luckyPipewrench/pipelock](https://github.com/luckyPipewrench/pipelock)
- [agentcage/agentcage](https://github.com/agentcage/agentcage)
- [spinningfactory/kloak](https://github.com/spinningfactory/kloak) and [Kloak: kernel-space secret injection via eBPF on Kubernetes](https://a-cup-of.coffee/blog/kloak/)
- [Squid `request_header_add` reference](https://www.squid-cache.org/Doc/config/request_header_add/)
