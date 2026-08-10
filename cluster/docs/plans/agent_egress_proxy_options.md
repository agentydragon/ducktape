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

Recommendation: **D for caching, and keep iron for the credential fences.** A
and B both put a cache and credentials in one process for a benefit the existing
artifact-cache pattern already delivers. Revisit A only if requirement 4 lands
on Squid anyway (see below), since that is the one thing Squid does that nothing
else here does.

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
