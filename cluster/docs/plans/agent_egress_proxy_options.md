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
| iron-proxy   | ❌              | ✅                       | ✅ glob + CIDR   | ⚠️ `judge`, LLM-only   |
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

## Finding: you cannot chain them either

The natural workaround is two proxies in series. Both orders are blocked:

- **Squid (cache) → iron (creds)**: Squid cannot forward _bumped_ traffic to a
  `cache_peer` parent. Upstream's own position is that multi-hop proxying with
  certificate mimicking "is difficult at best and not possible in current Squid
  versions"; Squid 4 can only splice TLS via `CONNECT` to parents, which defeats
  the bump the cache needs.
- **iron (creds) → Squid (cache)**: iron-proxy documents no `upstream_proxy` /
  `parent_proxy` / `HTTP_PROXY` handling. It dials upstreams directly.

Because caching HTTPS requires decrypting it, the cache **must be** the MITM
layer. "Add a cache behind the fence" is not available.

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
