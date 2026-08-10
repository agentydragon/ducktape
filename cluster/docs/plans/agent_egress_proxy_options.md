# Egress proxy options: caching + credential substitution + allowlisting

Survey, 2026-08-10. Wanted, on one fence:

1. HTTP response caching (RFC 9111)
2. Credential substitution (agent holds a placeholder; proxy swaps the real value)
3. Domain allowlisting, the more granular the better
4. Later: a hook letting haku-console decide, at request time, whether an
   _undecided_ domain is allowed — holding the request while an operator answers

## Finding: nothing on the market does 1 + 2 together

The two capabilities come from different lineages and no project spans them.
Caching lives in the traditional forward-proxy world (Squid); credential
substitution lives in the 2026 agent-egress world (iron-proxy, Infisical
agent-proxy, agentgateway, onecli, agentcage). Nothing in the second group
mentions HTTP caching at all — they are security appliances, and caching a
decrypted response next to injected credentials is a posture most of them
deliberately avoid.

|              | Cache           | Cred substitution        | Allowlist        | External decision hook |
| ------------ | --------------- | ------------------------ | ---------------- | ---------------------- |
| Squid        | ✅ mature       | ⚠️ fixed-string only     | ✅ granular ACLs | ✅ `external_acl_type` |
| iron-proxy   | ❌ (can chain)  | ✅                       | ✅ glob + CIDR   | ⚠️ `judge`, LLM-only   |
| mitmproxy    | ❌              | ⚠️ via own addon         | ❌               | ⚠️ via own addon       |
| agentgateway | ❌              | ✅ (CB4A Model A)        | not documented   | ✅ PDP                 |
| Envoy        | ⚠️ alpha filter | ✅ `credential_injector` | ✅               | ✅ `ext_authz`         |

Squid's nearest substitution primitive is `request_header_access` +
`request_header_replace`, which replaces a denied header "with some fixed
string" — no placeholder matching, no per-destination scoping beyond ACL
gymnastics, no secret sourcing, and no base64 `Basic` rewriting (the shape git
over HTTPS uses, and the reason iron beat our own mitmproxy addon).

Envoy was already tested here and rejected: its `credential_injector` works in
reverse-proxy mode but never sees requests inside a `CONNECT` tunnel, because it
has no dynamic-certificate machinery. See
<../../../plans/personal_agents/credential_proxy_options.md>. agentgateway is
the same shape — a gateway, not a transparent forward proxy for arbitrary
hostnames — so unmodified `git`/`pip`/`npm` against real hosts is out.

## Finding: chaining works, in one direction

An earlier draft of this note claimed both orders were blocked. That was wrong
about iron, and the correction matters because it reopens the composition:

- **Squid (cache) → iron (creds)**: genuinely blocked. Squid cannot forward
  _bumped_ traffic to a `cache_peer` parent; upstream's position is that
  multi-hop proxying with certificate mimicking "is difficult at best and not
  possible in current Squid versions", and Squid 4 can only splice TLS via
  `CONNECT` to parents, which defeats the bump the cache needs.
- **iron (creds) → Squid (cache)**: **supported.** `proxy.upstream_proxy` is a
  real config key, undocumented in the README but present in
  `internal/config/upstream_proxy.go`:

  ```yaml
  proxy:
    upstream_proxy:
      http_proxy: "http://cache:3128"
      https_proxy: "http://cache:3128"
      no_proxy: "localhost,127.0.0.1"
  ```

  `http`, `https`, `socks5` and `socks5h` schemes are accepted; there is no
  proxy-auth field. Verified in-cluster: iron starts cleanly with the key set
  (config parsing is strict — `dec.KnownFields(true)` — so an invalid key fails
  loudly), and a request then produces an upstream dial to the configured proxy
  rather than to the origin.

  Two gotchas found while testing:
  - **`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` env vars override the config.**
    Anything that injects proxy env into an iron pod silently re-points its
    egress. Kyverno already injects exactly these into every `haku-sandbox` pod.
  - **`upstream_deny_cidrs` defaults block `127.0.0.0/8`**, so an upstream proxy
    on loopback is refused by iron's own SSRF guard. A sidecar reached over the
    pod IP is fine; same-container loopback is not.

So the viable shape is **workload → iron (bump, substitute, allowlist) → Squid
(bump again, cache) → origin**, with Squid in its ordinary bumping role and no
`cache_peer` involved. Two-layer MITM, which is what makes it work.

The cost is not free: Squid sits _after_ substitution, so it sees the real
credentials in plaintext and would cache responses to authenticated requests.
RFC 9111 already forbids caching those absent explicit opt-in, and both hops
stay inside the cluster — but the credential is exposed to a second component,
which is the property iron exists to protect.

Note the ordering constraint is the opposite of what security would prefer:
Squid-first would keep credentials away from the cache, and that is the order
Squid cannot do.

## Target architecture (operator, 2026-08-10)

**One central caching proxy, with the per-consumer iron-proxies in front of it**,
and possibly a haku-console decision hook later:

```text
workload ──▶ iron (bump, substitute, allowlist) ──▶ central cache ──▶ origin
             one per fence, own credentials          shared by all fences
```

This is the only order that works (Squid cannot be first — see above), and
sharing one cache across every fence amortises it while each iron keeps its own
credential set and host list. It also gives row 1 a path to an L7 allowlist and
caching at once, which is what started this.

### Decided: secrets may transit the shared cache

A central cache behind every iron sees post-substitution credentials from **all**
fences, aggregating in one component what the separate listeners and per-proxy
CNPs otherwise keep apart. **The operator accepts this (2026-08-10)** — the cache
is reviewed in-cluster infrastructure and trusted for that role.

That is a real simplification, and it is what makes the decision hook work at
all (below). The alternative, kept here in case the posture changes, is
`upstream_proxy.no_proxy`: point each iron's bypass list at exactly the hosts
where it substitutes a credential, so the shared cache only ever handles
anonymous artifact traffic.

```yaml
proxy:
  upstream_proxy:
    https_proxy: "http://egress-cache:3128"
    no_proxy: "api.anthropic.com,api.github.com,forgejo-http.forgejo" # if ever wanted
```

Nothing would be lost by that bypass — RFC 9111 forbids caching responses to
authenticated requests, so those hosts produce no hits either way.

### Transit is trusted; storage should still be denied

Accepting a credential **in transit** is not the same as accepting it **at
rest**, and the second is avoidable at zero cost. Squid must not write
authenticated responses to its cache directory, where they would outlive the
request and survive a pod restart.

RFC 9111 already forbids it by default, so this is about not accidentally
overriding it:

- Do not add `ignore-auth`, `ignore-no-store`, `ignore-private`, or
  `override-expire` to any `refresh_pattern`. Those are the exact knobs that
  turn "sees the secret" into "stores the response".
- Prefer `cache deny` for the credentialed hosts explicitly, rather than relying
  on origin headers being correct.
- Consider memory-only (`cache_mem` with no `cache_dir`) for the first
  iteration. It costs hit rate across restarts and removes on-disk persistence
  of anything.

Worth an explicit test, since a wrong `refresh_pattern` fails silently and looks
like a cache-tuning win.

### Requirement 4 lands at the cache, and the decision above is what allows it

With every fence's traffic transiting the shared cache — credentialed included —
one `external_acl_type` helper there sees **every** domain access attempt in the
cluster. That is the whole requirement, at a single gate point, using the only
mechanism surveyed that can both hold a request for a human and remember the
answer:

- the helper answers `OK` / `ERR` / `BH` and may take as long as it needs;
- Squid caches the verdict via `ttl=` (default 3600s) and `negative_ttl=`, so an
  operator is asked once per domain, not once per request;
- `concurrency=n` keeps one helper serving interleaved queries.

Had credentialed traffic bypassed the cache, this hook would have been blind to
exactly the domains most worth gating, and the gate would have had to live in
iron — whose only decision hook is `judge`: LLM-provider-only, `8s` timeout,
deny-on-timeout, no deferral, and therefore unable to wait for a human. So the
trust decision above is load-bearing for this feature, not merely a
simplification.

Implementation is then mostly wiring, because **haku-console already has the
hard half**: its MCP tool-call approval queue is these semantics exactly —
synchronous wait, `pending_approval` stub, later approve/deny, execute if
approved. The helper is a Python process that turns a Squid ACL query into a
console approval request. Domain gating becomes a new caller of an existing
mechanism.

Copy the UX from Claude Code's own sandbox: prompt on first access to a new
host, allow for the session, with a pre-approved list and a strict-deny mode so
neither prompting nor blocking is mandatory.

#### Caller identity is lost at iron, not at the cache

The approval prompt needs to say _who_ is asking. "Something wants
`example.com`" is close to unactionable; "the openclaw spike wants
`example.com`" is a decision an operator can make in a second.

Identity does not survive the first hop. iron knows the caller — its audit
records `remote_addr` as the client pod, confirmed in the in-cluster test — but
it then opens its **own** upstream connection, so the cache sees only the iron
pod's address. What reaches an `external_acl_type` helper via `%SRC` is
therefore _which fence_, not which workload:

| Fence                                        | Distinguishable at the cache?                                         |
| -------------------------------------------- | --------------------------------------------------------------------- |
| `haku-claude-sandbox` / spike / public-coder | Yes — separate iron pods, separate source IPs                         |
| Inside `haku-sandbox`                        | **No** — every sandbox pod, haku-ci runner and haku-ui share one iron |

So the granularity is worst exactly where it matters most: the busiest fence,
the one with many distinct agents behind a single proxy.

No existing transform closes this. The documented set is `allowlist`, `secrets`,
`body_capture`, `annotate`, `header_allowlist`, `judge`, `mcp`, `mcp_gateway` —
`annotate` _captures_ headers into the audit record and `secrets` _replaces_ a
placeholder it finds; neither injects a new header of iron's own.

Four ways to get identity to the gate:

1. **Ask upstream to forward caller identity** — an option making iron stamp
   something like `X-Iron-Client: <remote_addr>` on upstream requests, which
   Squid reads with a `%{X-Iron-Client}>h` format code. Small feature, and it
   belongs in the same upstream conversation as the decision hook and caching;
   all three are the same gap.
2. **Gate at iron instead**, where identity is already known — blocked today,
   because `judge` cannot hold a request for a human. Solved by the same
   upstream ask (a generic webhook transform), which would then make the
   cache-side hook unnecessary.
3. **One iron per agent**, as a sidecar rather than a shared fence proxy. Pod IP
   becomes the identity, `%SRC` is sufficient, and each iron holds only that
   agent's credentials — better scoping than the fence model. Costs a proxy per
   agent pod, per-agent config, and CA plumbing. This is a documented pattern in
   the 2026 agent-egress literature, not an invention.
4. **Accept fence-level granularity.** Adequate for the three single-tenant
   fences; inadequate inside `haku-sandbox`.

Options 1 and 2 are the same upstream request seen from two ends, and either
removes the need for the other. Worth resolving before building the helper,
since it decides whether the helper lives at the cache or at iron.

## Options, ranked

**A. Squid + an ICAP service for substitution.** Squid natively covers 1, 3 and
4; substitution moves into an ICAP/eCAP service that sees the bumped request and
rewrites headers. One data path, no chaining, no double-bump. Costs: an
OpenSSL-built Squid image (`ubuntu/squid` is gnutls and rejects `ssl-bump`), a
PVC for the cache, and an ICAP service that now holds the credentials — which is
the "forty lines of ours" objection that pushed us to iron in the first place,
relocated.

**B. Add caching to iron-proxy.** We already build it from a pinned commit with
our own workflow (<../../images/iron-proxy/>), so a fork or upstream PR is not
exotic. Keeps substitution, allowlist and audit exactly as they are. Costs: real
Go work; caching bodies undoes the streaming property that keeps memory bounded
on large artifacts; and it puts a cache in a credential-holding pod.

**C. Two proxies, split by traffic class, no chain.** Cacheable artifact hosts
(PyPI, npm, Bazel, nixos) point at Squid; credential hosts (Forgejo, GitHub,
Anthropic, console, kubeapi) point at iron. No new code. Costs: per-tool proxy
configuration instead of one `HTTPS_PROXY`, and GitHub straddles both classes
(codeload tarballs are cacheable, the API needs the PAT).

**D. Skip HTTP caching; cache at the artifact layer.** devpi/Verdaccio-shaped
pull-through caches beside the existing `nix-cache`, `oci-cache` and
`harbor/proxy-cache`. Content-addressed and deduplicating, so it caches better
than RFC 9111 would. Costs: more components, none of them novel. Also shrinks
the allowlists, since the fence then permits one internal host instead of five
external ones.

## Decision (operator, 2026-08-10): B

The requirements are **not equal in weight**. Credential substitution is the
hard one; caching is wanted but secondary. And where caching happens it should
be **at the HTTP layer**, because RFC 9111 works for any origin, whereas an
artifact cache has to be built and operated once per ecosystem (PyPI, npm,
Bazel, …) and only ever covers the ecosystems someone got around to.

That ordering settles it. **A inverts the priorities** — it makes the hard
requirement the bolted-on ICAP service and the soft one native. **D was the
cheap answer and is explicitly not what is wanted**: it trades genericity for
components that already exist, which is a reasonable trade to decline. **C**
leaves the fence split across two proxies with per-tool configuration.

So: **keep iron-proxy and teach it HTTP caching.** Costs stay as listed under
B — Go work, and a cache living in a credential-holding pod.

### Why B is more tractable than it looks

- iron's transform pipeline **already short-circuits**. `allowlist` and `judge`
  return a response without reaching upstream, so "serve this from cache" is
  expressible in the existing model rather than needing a new one.
- Go has mature RFC 9111 implementations to vendor rather than write.
- The streaming property survives if the cache takes a **size cap**: cache below
  it, stream above it. That is standard practice (Squid's `maximum_object_size`)
  and it keeps the large-artifact path — the one that OOM-killed mitmproxy —
  untouched.

### Do these two things before writing any Go

1. **Measure the hit rate first.** The `annotate` transform captures HTTP
   headers into audit annotations, and the audit log already exports over OTLP.
   If `annotate` covers _response_ headers (verify — the docs are not explicit),
   recording `Cache-Control`/`ETag`/`Age` for a week costs one config block and
   zero code, and tells us what fraction of bytes is actually cacheable. Some of
   the heaviest traffic redirects to CDNs with signed URLs, which may not cache
   at all. Caching is the secondary requirement; do not spend Go time on it
   before knowing the payoff.
2. **Ask upstream.** iron ships release candidates weekly and is clearly
   actively developed. An issue asking about RFC 9111 caching may find it
   planned, wanted, or already prototyped — and upstreaming beats carrying a
   fork, given we already maintain a commit-pinned build.

## Requirement 4: the haku-console decision hook

This is the strongest reason to run Squid somewhere, and it is worth separating
from the caching question.

**Squid `external_acl_type` is a direct fit.** A helper process receives
formatted request fields and answers `OK` / `ERR` / `BH`. Decisions are cached
by Squid itself — `ttl=n` (default 3600s) and a separate `negative_ttl` — and
`concurrency=n` lets one helper handle interleaved queries with channel IDs. So
a Python helper can call haku-console on an undecided domain, wait for the
operator, answer, and Squid remembers the verdict for the whole TTL rather than
re-asking per request. That is exactly the requested behaviour.

**iron's `judge` transform is not a fit, despite appearances.** It is hardcoded
to Anthropic/OpenAI providers, with `timeout` defaulting to `8s`, a circuit
breaker, and `fallback: deny`. Nothing about deferral, human approval, or
decision caching is documented. An 8-second ceiling with deny-on-timeout cannot
hold a request for a human.

There is a loophole worth recording: the provider block accepts an optional
`base_url`, so haku-console could expose an Anthropic-Messages-compatible
endpoint and receive judge calls. It would still be bounded by the judge
timeout, so it suits _automated_ policy (haku-console decides in code) but not
operator-in-the-loop approval.

Note that haku-console **already implements the hard half**: its MCP tool-call
approval queue has exactly these semantics — synchronous wait, `pending_approval`
stub, later approve/deny, execute if approved. Extending that queue to
"domain X requested by workload Y" is a new caller, not a new mechanism.

Prior art for the UX: Claude Code's own sandbox prompts on first access to a new
host and then allows it for the session, with `allowedDomains` to pre-approve
and `strictAllowlist` to deny instead of prompting. Same shape, worth copying
including the pre-approve and never-prompt escape hatches.

## Standards context

The IETF draft **CB4A** ("Credential Broker 4 Agents", March 2026) formalises
this pattern; agentgateway implements its Model A, where the agent calls without
a credential and something on the path attaches the real one. Our iron fences
are already Model A in all but name, so the vocabulary is worth adopting if this
area grows.

## Sources

- <https://agentgateway.dev/blog/2026-07-27-credential-injection-ai-agent-egress-cb4a/>
- <https://infisical.com/blog/agent-proxy>
- <https://hermes-agent.nousresearch.com/docs/user-guide/egress/iron-proxy>
- <http://www.squid-cache.org/Doc/config/external_acl_type/>
- <http://www.squid-cache.org/Doc/config/request_header_replace/>
- <https://code.claude.com/docs/en/sandboxing>
- <https://www.innoq.com/en/blog/2026/03/dev-sandbox-network/>
