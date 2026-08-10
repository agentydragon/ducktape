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

### Why Envoy and agentgateway are out

Both fail the same way, but the evidence is much stronger for one than the other.

**Envoy — rejected empirically**, in
<../../../plans/personal_agents/credential_proxy_options.md>. Same Envoy, same
credential, same `credential_injector` filter, two listeners. The reverse-proxy
leg worked (`GET /user` → 200 as the bot account, `PATCH` → 403 from the RBAC
filter). The forward-proxy leg:

```text
curl -x envoy:8081 https://api.github.com/user  ->  401 "Requires authentication"
```

The 401 is the whole answer: Envoy sees an opaque TLS tunnel and has no
dynamic-certificate machinery, so the filter never receives a request to act on.
Because the reverse leg proves the filter and credential were configured
correctly, the forward failure is structural rather than a misconfiguration.

**agentgateway — rejected on architecture**, not on a test. Its model is
`bind → listener → route → backend`, where a bind is a TCP port, a listener does
hostname matching and **TLS termination**, and a backend is a _configured
upstream_ (an MCP server, an A2A agent, an HTTP service, an AI provider). A
forward proxy has no configured upstream — the client chooses the host at
request time — and TLS termination is not per-host certificate generation. So it
is a gateway, which puts it in camp 1 of the taxonomy in that same document:
fine when the client's endpoint is a knob (an SDK `base_url`), useless when the
agent runs `git clone https://github.com/owner/repo`, because the hostname is
baked into remotes, into `gh`'s API host, and into every URL in every README it
reads.

Worth being fair to it: agentgateway is aimed at a genuinely different traffic
shape, and is strong at MCP/A2A proxying with tool-level policy. If Haku ever
wants MCP tool gating enforced at the network layer rather than in the console,
it is a real candidate for **that** — just not for the egress fence. (iron has
`mcp` / `mcp_gateway` transforms covering similar ground.)

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

**iron forwards no client identity at all** — confirmed in
`internal/proxy/proxy.go`, not inferred from docs. It builds the upstream
request by hand rather than using `httputil.ReverseProxy`, which would have
added `X-Forwarded-For` for free:

```go
upstreamReq, err := http.NewRequestWithContext(r.Context(), r.Method, upstreamURL, ...)
copyHeaders(upstreamReq.Header, r.Header)
sanitizeUpstreamHeaders(upstreamReq.Header)
```

No `X-Forwarded-For`, `X-Real-IP`, `Via` or `Forwarded` is set;
`sanitizeUpstreamHeaders` only strips hop-by-hop headers. `RemoteAddr` reaches
the audit `PipelineResult` and nothing else.

Nor does any transform close the gap: the documented set is `allowlist`,
`secrets`, `body_capture`, `annotate`, `header_allowlist`, `judge`, `mcp`,
`mcp_gateway` — `annotate` _captures_ headers into the audit record and
`secrets` _replaces_ a placeholder it finds; neither injects a header of iron's
own.

**The trap**: `copyHeaders` passes the client's own headers through verbatim, so
a workload setting `X-Agent-Id: whoever` will see it arrive at the cache. That
identity is asserted by the very thing being gated, so it is forgeable —
worthless for an authorization decision, and worse than nothing if a helper
trusts it, since a prompt-injected agent could impersonate a more privileged
one. Identity for this gate has to be stamped by the proxy, which is exactly
what iron does not do today.

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

## Option E: one Squid per fence, doing everything

The chained designs above stack proxies because each product has half the
feature set. Worth asking the other question: **how hard is iron's substitution
inside Squid?** If Squid can do it, the stack collapses to a single layer.

### The mechanism exists, natively

Not `request_header_replace` — that replaces a denied header with "some fixed
string", one value per header name, no ACLs. The usable pair is:

```squid
acl to_github  dstdomain api.github.com github.com codeload.github.com
acl to_forgejo dstdomain forgejo-http.forgejo

request_header_access Authorization deny all                    # drop whatever the agent sent
request_header_add    Authorization "Bearer ghp_…"  to_github   # inject per destination
request_header_add    Authorization "Basic <b64>"   to_forgejo
```

`request_header_add field-name field-value [ acl ... ]` — "One or more Squid
ACLs may be specified to restrict header injection to matching requests", so one
directive per destination gives per-destination values. Strip-then-add
reproduces substitution semantics.

**Bonus the chained design cannot offer**: the documented example is
`request_header_add X-Client-CA "CA=%ssl::>cert_issuer" all`, i.e. **format
codes are allowed in the value**. So Squid can stamp _proxy-asserted_ client
identity (`%>a`) — unforgeable, and exactly the thing iron does not do. The
identity problem above disappears in this design rather than needing an upstream
feature.

### What is given up versus iron

- **Secret sourcing.** The value lives in `squid.conf`, not read from env or a
  vault with a TTL. Render it at pod start from the Secret (initContainer +
  `envsubst` onto an in-memory emptyDir); rotation becomes re-render + reload,
  which `reloader.stakater.com/auto` already does here.
- **Placeholder semantics need a second directive** (see below). Naive
  strip-and-add is not equivalent to substitution and should not be accepted as
  such.
- **Base64 `Basic` is precomputed**, not rewritten in flight — the whole
  `Basic <b64(user:secret)>` string is rendered at config time. Fine for static
  per-host credentials; it does not generalise the way iron's rule engine does.
- **Auditing.** `access.log` against iron's structured JSON with per-transform
  traces. The OTLP path would need rebuilding.
- **Focus.** Squid is a large C++ codebase with a long CVE history, against a
  small Go binary built for this job. And "substitution as a maintained
  project's product rather than forty lines of ours" was the stated reason iron
  beat our mitmproxy addon — `squid.conf` directives are far less code than an
  addon, but the argument is not zero.

### Substitution semantics matter, and are expressible

Unconditional strip-and-add is **not** what iron does, and the difference is
semantic rather than cosmetic. With substitution the agent _asks_ for the
credential by presenting the placeholder; its absence means "send this
unauthenticated" or "I am using my own token", and the proxy honours that.
Strip-and-add erases the distinction — every request to `github.com` carries the
PAT, so there is no anonymous clone of a public repo, no second account, no
user-supplied token for one task, and the credential is attached to paths the
agent never intended to authenticate.

Squid can express the real semantics with a header-matching ACL, so the
placeholder gates the injection:

```squid
acl gh_placeholder req_header Authorization -i ^Bearer\ proxy-github-placeholder$
request_header_access Authorization deny gh_placeholder
request_header_add    Authorization "Bearer ghp_…" gh_placeholder
```

Requests without the placeholder pass through untouched — anonymous stays
anonymous, and an agent-supplied token is left alone. For git over HTTPS the
same works against the base64 form, since both the placeholder and the real
value are static and can be rendered at config time.

**Second unknown for the spike**: whether `request_header_add`'s ACL still
matches after `request_header_access` has denied the header, or whether the deny
removes the value the ACL needs. If the ordering defeats it, the fallbacks are
`request_header_replace` — which is _defined_ as acting on denied headers, but
allows only one value per header name, so it fits a fence carrying a single
credential (claude, public-coder) and not the openclaw spike's five — or an
ICAP service.

### Why this is _less_ stacking, not more

Squid cannot chain to Squid: the `cache_peer`-with-`ssl_bump` limitation applies
to any Squid in a first hop, so "per-fence Squid → central caching Squid" is
**not available**. An all-Squid design therefore has to be **one Squid per
fence doing all four jobs** — substitute, allowlist, gate, cache.

That is a single layer, where the iron design needs two. The cost is that each
fence keeps its own cache instead of sharing one central cache: more disk, lower
hit rate, no cross-fence reuse of the same PyPI wheel.

|                             | iron → central Squid             | one Squid per fence          |
| --------------------------- | -------------------------------- | ---------------------------- |
| Layers                      | 2                                | **1**                        |
| Cache sharing               | **shared**                       | per fence                    |
| Caller identity at the gate | needs an upstream feature        | **native (`%>a`)**           |
| Substitution quality        | **rule engine, secret backends** | strip + add, config-rendered |
| Credential exposure         | cache sees it (accepted)         | **stays in one process**     |

### SPIKE RESULT (2026-08-10): both questions answered YES

Run in `haku-sandbox` against `alatas/squid-alpine-ssl` (Squid 3.5.27, built
`--with-openssl --enable-ssl-crtd`), bumping a local `openssl s_server` origin,
with the exact strip-and-add-gated-by-`req_header` config from above. Outgoing
headers observed from inside Squid via `debug_options ALL,1 11,2`, which logs
the literal request sent to the origin.

Squid bumped the connection — the access log shows the inner request, which only
exists after decryption:

```text
TAG_NONE/200   CONNECT origin.test:8443
TCP_MISS/200   GET https://origin.test:8443/
```

Three client requests through the bumped tunnel, and what reached the origin:

| Client sent                             | Squid sent to origin                                |
| --------------------------------------- | --------------------------------------------------- |
| `Authorization: Bearer PLACEHOLDER`     | `Authorization: Bearer REAL-SECRET-INJECTED`        |
| `Authorization: Bearer AGENT-OWN-TOKEN` | `Authorization: Bearer AGENT-OWN-TOKEN` — untouched |
| _(no Authorization)_                    | _(none)_ — stays anonymous                          |

All three also received `X-Proxy-Stamped-Client: 127.0.0.1` from the `%>a`
format code.

So, confirmed:

1. **`request_header_access` / `request_header_add` do apply to `ssl_bump`-
   decrypted requests.** This was the open question from
   `credential_proxy_options.md`, undocumented in both directions until now.
2. **The `add` ACL still matches after `access` denied the header**, so
   strip-then-add is a working substitution primitive, not just injection.
3. **Substitution semantics hold**: the placeholder gates the swap, an
   agent-supplied token passes through, and no header stays no header. This is
   the property that makes it equivalent to iron's `secrets` transform rather
   than a cruder "always attach the credential".
4. **Proxy-asserted identity works**, unforgeable and available to
   `external_acl_type` — the thing iron cannot do without an upstream feature.
   Squid also adds `X-Forwarded-For` of its own accord.

**Option E is therefore live**, and it is the only route that satisfies all four
requirements in a single layer with no upstream dependency.

#### Caveats on this result

- **Squid 3.5.27 (2017)**, because it is what pulled cleanly;
  `eraa/squid-ssl` (6.9) 404s. These directives are ancient and stable and the
  feature has not been removed, but a 6.x/7.x re-run before committing is cheap
  insurance.
- **Container needs `/dev/shm`** (emptyDir, `medium: Memory`) and
  `sslproxy_session_cache_size 0`; without them Squid dies at startup with
  `shm_open(/squid-ssl_session_cache.shm)` — a musl/container interaction, not a
  config error.
- **Not yet tested**: more than one credential per fence (several `req_header`
  ACLs on the same header), the base64 `Basic` form for git-over-HTTPS, and
  anything about caching behaviour. None look risky, but none are proven.

## Cache storage: no S3, anywhere

Squid's `cache_dir` types are `ufs`, `aufs`, `diskd` and `rock` — local
filesystem directories, or a single database file. There is no object-store
backend, and no plugin interface that would add one.

Nor is this a Squid gap. **Souin**, the leading Go RFC 9111 cache and the
library option B would most likely vendor, supports Badger, Nuts, Otter, Olric,
Redis, Etcd and Nats — **no S3**. Varnish caches to memory or file, ATS to raw
disk volumes. The S3-plus-cache projects that do exist are the _inverse_ shape:
caching proxies that sit in front of S3 **as an origin**, not caches that store
their objects in S3.

The reason is structural. An HTTP cache does many small random reads under a
tight latency budget, with atomic overwrite and prompt eviction. S3 gives tens
of milliseconds per GET, no partial writes, and lazy delete semantics. The
industry answer to "durable, shared cache" is Redis or etcd, not object storage.

### Decided: per-Squid `emptyDir` cache, not shared (operator, 2026-08-10)

No PVC, no shared store, no cross-instance cache. Each proxy caches to its own
`emptyDir` and loses it on reschedule. This is the right trade, and it removes
work rather than adding it:

- **No node pinning.** `emptyDir` follows the pod anywhere, unlike
  `local-path-*`. A cache is disposable, so a reschedule costs hit rate, not
  data.
- **No sibling mesh.** Squid can share between instances via ICP, HTCP or cache
  digests, but declining to means the open question _"does sibling peering
  survive `ssl_bump`?"_ — plausibly blocked by the same limitation as
  `cache_peer` parents — never has to be answered. One less unknown on the R2
  path.

**Gotcha to configure carefully**: `emptyDir` consumes the node's ephemeral
storage, and exceeding its `sizeLimit` **evicts the pod**. Squid's `cache_dir`
maximum must sit comfortably below the `sizeLimit`, with headroom for
`cache.log`, `access.log` and the `ssl_db` certificate store — which also needs
writable space and can share the volume. A cache sized to its own limit evicts
the proxy under load, presenting as a mysterious egress outage rather than a
full disk.

Memory (`cache_mem`, no `cache_dir`) stays the simplest first iteration: it
persists nothing, so it also settles the credential-at-rest concern for free.
`emptyDir` disk is the second step, when RAM is the binding constraint.

Valkey stays available to option B (Souin speaks go-redis) but is now moot for
the comparison: with sharing declined, Squid's lack of a pluggable store costs
nothing.

## Are there better-matched proxies? No — and the reason is worth knowing

The eliminating constraint is **dynamic per-host certificate minting in forward
proxy mode**. Caching HTTPS requires decrypting it, which requires forging a
cert for an arbitrary origin on the fly. Almost nothing does this:

| Candidate                               | Why it is out                                                                                                                                          |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Apache Traffic Server                   | Caching forward proxy with a real plugin API, but no per-host cert mimicry; its TLS Bridge plugin tunnels between two ATS instances, which is not MITM |
| nginx + `ngx_http_proxy_connect_module` | Does `CONNECT` and `proxy_cache`, but no dynamic cert generation, so HTTPS stays an opaque tunnel and is uncacheable                                   |
| Varnish / nuster                        | Reverse-proxy shaped; no forward `CONNECT`, no MITM                                                                                                    |
| Envoy / agentgateway                    | Already tested and rejected here for exactly this — no dynamic-certificate machinery inside `CONNECT`                                                  |
| mitmproxy                               | MITMs, but no cache, and substitution means the bespoke addon already rejected                                                                         |
| Caddy + forwardproxy + Souin            | The one plausible assemble-from-Go-parts route, but it is a stack to build, not a product to run                                                       |

So the field really is Squid and iron, which is why the survey found no product
at the intersection. Everything mature either MITMs without caching, or caches
without MITM.

That makes the choice narrower than "pick the best tool": either teach the
MITM-capable Go proxy to cache (B), or teach the caching MITM proxy to
substitute (E). No third product is waiting to be found.

## Routes, and what gates each

Three end-states are reachable. Each satisfies the four requirements to a
different degree, and each is gated on something cheap that has not been done
yet — so **the next step is an experiment, not a build**.

|                               | 1. Cache     | 2. Substitution | 3. Allowlist | 4. Console hook | Identity at the gate |
| ----------------------------- | ------------ | --------------- | ------------ | --------------- | -------------------- |
| **R1** iron → central Squid   | ✅ shared    | ✅ full         | ✅           | ⚠️ at Squid     | ⚠️ fence-level only  |
| **R2** Squid alone, per fence | ✅ per fence | ⚠️ spike        | ✅           | ✅              | ✅ native `%>a`      |
| **R3** iron alone, per fence  | ⚠️ build     | ✅ full         | ✅           | ⚠️ upstream     | ⚠️ upstream          |

**R1 — two layers, buildable now.** iron keeps substitution and allowlisting;
one shared Squid behind it caches. Needs no new code: `upstream_proxy` is
verified working, and the only missing artifact is an OpenSSL-built Squid image.
Weakest on requirement 4 — the hook sees which fence, not which workload.

**R2 — one layer, everything in Squid.** Collapses the stack, and uniquely gets
unforgeable caller identity for free (format codes in `request_header_add`).
Gated on the `ssl_bump` spike. Costs iron's structured audit and secret
backends, and gives each fence its own cache.

**R3 — one layer, everything in iron.** Keeps every property currently liked,
and Valkey is already available as a Souin backend. Gated on upstream appetite
for two features (RFC 9111 caching, a generic decision hook with caller
identity) or on carrying a fork — which is less exotic here than elsewhere,
since a commit-pinned build is already maintained.

**Identity is separable.** Per-agent iron _sidecars_ make the pod IP the
identity and fix requirement 4 for R1 or R3 without any upstream change. Costs a
proxy per agent pod; gives per-agent credential scoping as a bonus.

### The two experiments that decide it

Both are ~a day, independent, and can run before committing to anything:

1. ~~The Squid spike~~ — **DONE 2026-08-10, both answers yes** (see the spike
   result under option E). R2 is live, meets all four requirements in one layer,
   and needs nothing from upstream. It is the front-runner.
2. **The upstream conversation** — one iron issue covering caching, a generic
   decision hook, and forwarded caller identity. **Receptive → R3.** Silence →
   R3 means a fork.

Measuring the actual cache hit rate (via `annotate` + the existing OTLP audit
export) is worth doing alongside, since caching is the secondary requirement and
some heavy traffic redirects to signed CDN URLs that will not cache at all.

### What does not depend on any of this

The gap that started this thread — `haku-sandbox` and `haku-ci` having **no L7
allowlist**, only the Cilium `toFQDNs` layer — is real but not urgent, and it is
already the standing TODO in `test_egress_allowlists`: _"enforce at two layers,
not one."_ Every route above ends with row 1 behind a proxy that has an L7
allowlist, so closing it early only risks moving it twice. Worth doing now only
if the single-layer posture is judged unacceptable before the routes resolve.

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
