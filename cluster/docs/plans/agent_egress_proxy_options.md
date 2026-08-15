# Egress proxy options: caching + credential substitution + allowlisting

Survey, 2026-08-10. Wanted, on one fence:

1. HTTP response caching (RFC 9111)
2. Credential substitution (agent holds a placeholder; proxy swaps the real value)
3. Domain allowlisting, the more granular the better
4. Later: a hook letting haku-console decide, at request time, whether an
   _undecided_ domain is allowed — holding the request while an operator answers

## Where this landed (read this first)

This note was written as the investigation ran, so several sections record
decisions that later ones supersede. Current position:

- **Route: option E / R2 — one Squid per fence doing all four jobs.** Proven by
  the spike below: header substitution works on `ssl_bump`-decrypted requests,
  with real placeholder semantics, plus proxy-stamped caller identity. Single
  layer, nothing needed from upstream.
- **Cache: per-fence `emptyDir`, not shared.** Memory-only first. No PVC, no
  sibling mesh, no Valkey.
- **Secrets may transit the cache** — accepted; the cache is trusted for that
  role. Storage of authenticated responses is still denied.
- **The console gate does not cache in Squid** (`ttl=0 negative_ttl=0`). The
  console is the only decision cache. See "Decided: do not cache the helper's
  response".
- **The image has ICAP now** (Debian `squid-openssl`, #4025). Substitution can
  therefore be a service call rather than generated config, which reopens the
  shared-Squid option — see "ICAP: substitution can be a service call". Compiled
  in, not yet exercised.
- **Superseded**: the two-layer "iron in front of a central shared cache" target
  architecture, and the earlier "Decision: option B". Both are kept below for
  their reasoning and are labelled.

The questions this note opened with are answered. The spike ran in-cluster on
Squid 7.6 (2026-08-10) and settled the 6.x/7.x port, several credentials per
fence, the base64 `Basic` form for git-over-HTTPS, destination scoping in both
directions, and `cache deny has_auth`. What remains is building the console
gate, not deciding whether the mechanism exists.

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

## Superseded: two-layer target architecture (iron → central cache)

> **Superseded** by option E after the spike. Kept because the credential-flow
> reasoning below still applies, and because it is the fallback if a Squid 6.x
> re-run contradicts the spike. The cache is no longer central or shared.

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

### Decided: secrets may transit the cache (still current)

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

### Requirement 4 lands at the cache (still current, and easier under option E)

With every fence's traffic transiting the shared cache — credentialed included —
one `external_acl_type` helper there sees **every** domain access attempt in the
cluster. That is the whole requirement, at a single gate point, using the only
mechanism surveyed that can both hold a request for a human and remember the
answer:

- the helper answers `OK` / `ERR` / `BH` and may take as long as it needs;
- an operator is asked once per domain rather than once per request — though
  **not** via Squid's `ttl=`, which is disabled: see "Decided: do not cache the
  helper's response". The console's own policy table is what remembers;
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
pod's address. What reaches an `external_acl_type` helper via `%>a` is
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
   becomes the identity, `%>a` is sufficient, and each iron holds only that
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

- **Secret sourcing needs a render step.** Squid does **not** read environment
  variables: the `${...}` syntax in `squid.conf` is limited to its own
  `${process_name}` / `${process_number}` / `${service_name}` macros. iron reads
  `api_key_env` and file/AWS/1Password sources with TTLs natively; Squid cannot.

  The wiring that works, and keeps the credential out of both git and etcd:
  1. **ConfigMap** holds the bulk `squid.conf`, credential-free, ending in
     `include /run/squid-secrets/credentials.conf` — `include` is a real Squid
     directive that recurses into another file at that position.
  2. **initContainer** renders only the credential-bearing lines (the
     `request_header_add` directives) from Secret-backed env vars via `envsubst`
     into an `emptyDir{medium: Memory}` shared with the Squid container. tmpfs,
     so the secret never touches disk, and never enters a ConfigMap.
  3. **Rotation** is re-render plus restart, which `reloader.stakater.com/auto`
     already drives on the existing iron deployments when their Secret changes.

  Two notes on that step. Rendering is also where the base64 `Basic` value for
  git-over-HTTPS gets computed, since both halves are static. And
  `configuration_includes_quoted_values` governs quoted parameters — values with
  spaces like `"Bearer ghp_…"` rely on it, though the spike's config used quoted
  values without setting it explicitly and Squid accepted them.

  **Do not** render the finished config into a ConfigMap: that puts the
  credential in plaintext in etcd and, if generated, in git.

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

#### SECURITY: gate on placeholder AND destination, never placeholder alone

The spike config matched the placeholder only:

```squid
acl ph req_header Authorization -i ^Bearer\ PLACEHOLDER$
request_header_add Authorization "Bearer REAL-SECRET" ph        # WRONG
```

That is an **exfiltration primitive**. The agent holds the placeholder — it is in
its own environment by design — so it can send the placeholder to any reachable
host and Squid will swap in the real credential on the way out. The placeholder
stops being a way to _request_ a credential and becomes a way to _extract_ one.

iron does not have this hazard because its `secrets` transform takes an explicit
`rules: - host:` per secret. The Squid form must AND a destination ACL; multiple
ACLs on one `request_header_add` line are ANDed:

```squid
acl ph_github  req_header Authorization -i ^Bearer\ proxy-github-placeholder$
acl to_github  dstdomain api.github.com github.com codeload.github.com
request_header_access Authorization deny ph_github
request_header_add    Authorization "Bearer ghp_…" ph_github to_github
```

The same applies to the strip: `request_header_access ... deny ph_github` without
a destination constraint drops the placeholder everywhere, which is harmless but
makes the intent harder to read.

Worth pinning with a test that asserts every `request_header_add` carrying a
credential also names a destination ACL — this is a single omitted token away
from a credential leak, and it will not fail visibly.

#### Caveats on this result

- **Squid 3.5.27 (2017)**, because it is what pulled cleanly;
  `eraa/squid-ssl` (6.9) 404s. The re-run on 6.x/7.x is worth doing, but it is a
  **config port rather than a semantics re-test**: the header directives are
  Squid 2.x-era and unchanged, while the surrounding TLS directives were renamed
  (`sslproxy_flags` → `tls_outgoing_options flags=`, `ssl_crtd` →
  `security_file_certgen`, the `sslproxy_*` family deprecated). Its real output
  is the `squid.conf` we would actually deploy — nobody is shipping an
  unmaintained 2017 build — plus confirmation the `/dev/shm` and session-cache
  workarounds still apply on a modern base.
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

## Superseded decision: option B

> **Superseded** by the option E spike result. The priority ordering below —
> substitution first, caching secondary, HTTP-layer over artifact-layer — is
> still the operator's, and still rules out options A, C and D. What changed is
> that E turned out to satisfy the same ordering in one layer, which B cannot.

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
formatted request fields and answers `OK` / `ERR` / `BH`, and `concurrency=n`
lets one helper handle interleaved queries with channel IDs. So a Python helper
can call haku-console on an undecided domain, get an answer, and enforce it.

Squid _can_ also cache the verdict itself (`ttl=n`, default 3600s, plus a
separate `negative_ttl`). **That is deliberately turned off** — see "Decided: do
not cache the helper's response". Remembering is the console's job, so that
revocation and approval both take effect on the next request.

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

## How haku-console would drive the filtering

Sketch of requirement 4 under option E, where one Squid per fence sees every
request from that fence's agents.

### Shape

```squid
# 1. Known-good domains never consult the helper at all.
acl known dstdomain "/etc/squid/allowed-domains.txt"
http_access allow known

# 2. Everything else asks haku-console, every time. No Squid-side cache.
#    ttl=0/negative_ttl=0 is the intent, not a verified spelling -- see the
#    UNVERIFIED note under "Decided: do not cache the helper's response".
#    %>a %>rd are logformat codes; the legacy %SRC/%DST spellings are deprecated.
external_acl_type haku_gate ttl=0 negative_ttl=0 concurrency=64 \
  %>a %>rd /usr/local/bin/haku_gate.py
acl gated external haku_gate
http_access allow gated

http_access deny all
```

The helper answers `OK` (match → allowed), `ERR` (no match → falls through to
`deny all`), or `BH` (helper broken). Every gated request consults the console;
the console answers already-decided pairs from its own table, so this is an RPC
per request, not a prompt per request.

#### What the helper actually receives

One line per query on stdin: the `FORMAT` codes from the `external_acl_type`
line, expanded, space-separated. Three properties to write the parser against:

- **Every value is URL-escaped** — "Request values sent to the helper are URL
  escaped to protect each value in requests against whitespaces" — so unescape
  each field. (Not true under `protocol=2.5`, the Squid-2.5 compatibility mode.)
- **Missing data arrives as `-`**, not as an empty field, so the field count is
  stable.
- **`concurrency=n` prefixes a channel tag**, "a number between 0 and
  concurrency-1", which the response must echo:
  `[channel-ID] result keyword=value ...`.

Useful codes, all standard `logformat` ones — `>` means client-side, `<`
server-side:

| Code          | Meaning                              |
| ------------- | ------------------------------------ |
| `%>a` / `%>p` | Client source IP / port              |
| `%>A`         | Client FQDN                          |
| `%rm`         | Request method                       |
| `%ru`         | Full request URL, sanitized          |
| `%>rd`        | Request URL domain from client       |
| `%>rp`        | Request URL path, excluding hostname |
| `%>rs`/`%>rP` | Request URL scheme / port            |
| `%{Header}>h` | Any received request header          |

Plus two specific to this directive: `%ACL` (the ACL name under test) and
`%DATA` (the arguments from the `acl … external` line; `%#DATA` passes the whole
string as one token).

Response keywords are `user=`, `password=`, `message=` (surfaced as `%o` on the
error page), `tag=`, `log=` (reaches access.log as `%ea`), and `clt_conn_tag=`.
**`ttl=` is not among them** — which is the documentary basis for the claim above
that a decision's expiry cannot be expressed to Squid.

**The static allowlist short-circuiting first is the load-bearing part.** It
keeps haku-console out of the path for all normal traffic, so console downtime
degrades "can reach a new domain" rather than "can reach anything". Without that
ordering, the console becomes a hard dependency of all egress.

### The constraint that shapes the UX

An earlier section of this note says `external_acl_type` "can hold a request for
a human". Squid's helper protocol does allow a slow answer — but **the agent's
HTTP client will not wait minutes**, and neither will Squid's own client-side
timeouts. So a request genuinely held pending operator approval fails at the
client regardless of what the helper eventually says.

The workable pattern is therefore **deny-now, ask-async, allow-on-retry**:

1. Undecided domain → helper files an approval with haku-console and returns
   `ERR` immediately (optionally after a short grace, ~5–10s, to catch an
   operator who is already looking).
2. Nothing to expire: with `negative_ttl=0` the denial is not cached at all.
3. The operator approves in the console; haku-console records it in its policy
   table.
4. The agent's retry — or its next run — hits the helper again and gets an
   immediate allow. Approval takes effect on the very next request.

That is Claude Code's sandbox UX adapted to a client that cannot be prompted:
the first attempt fails, approval happens out of band, the retry succeeds.

### Why this is mostly wiring

haku-console already implements the hard half. Its MCP tool-call approval queue
is exactly these semantics — synchronous wait, `pending_approval` stub, later
approve/deny, decision recorded — so domain gating is a **new caller of an
existing mechanism**, not a new mechanism. The helper is a small Python process
translating a Squid ACL query into a console approval request.

Durability splits the right way too: **haku-console owns the decisions**, Squid's
ACL cache is only an in-memory optimisation that dies with the pod. After a
restart the helper re-asks and the console answers from its table without
troubling the operator.

### Two design decisions to make before building

**~~What goes in the cache key.~~** Settled by the no-cache decision above: there
is no Squid-side cache to key. `%>a %>rd` are simply what the helper is told
about each request, and scoping — per agent, per host, per time box — is entirely
the console's to define in its own policy table.

**Fail-closed versus fail-open.** `ERR` on console failure fails closed, which is
right for a security control — and is survivable precisely because the static
allowlist above never consults the helper. `BH` is the honest signal for "helper
broken" and should be distinguished from a genuine deny in the audit, otherwise
an outage looks like a policy decision.

### Target behaviour (operator, eventually)

1. **Depend on haku-console; deny if it is down.** The console is the policy
   authority, not an optimisation.
2. **Bounded wait**: the helper gives the console a configured time; an answer
   inside that window is honoured, otherwise deny.
3. **haku-console maintains time-boxed, agent-scoped decisions.**

This supersedes the "static allowlist short-circuits so the console is never a
hard dependency" suggestion above. It is a coherent fail-closed posture; the
consequences below are what it costs.

#### Decided: do not cache the helper's response (operator, 2026-08-10)

`ttl=0 negative_ttl=0`. Squid holds no verdicts; **haku-console is the only
decision cache**, and it is authoritative.

> **UNVERIFIED — the exact directive that disables the cache.** The intent above
> is settled; the spelling is not. Squid documents `ttl=n` as "TTL in seconds for
> cached results (defaults to 3600)", `negative_ttl` as defaulting to the same,
> and a _separate_ `cache=n` as "the maximum number of entries in the result
> cache". **Nothing in the documentation says `0` disables caching** for either
> knob, and `cache=0` may equally mean "unlimited". Confirm by observation before
> relying on it — issue the same gated request twice and check the helper is
> called twice. This is the `DONT_VERIFY_PEER` shape: a value that parses without
> complaint and may not mean what the config assumes.

This removes a constraint rather than adding cost. The helper **cannot** return a
per-response `ttl=` — the response keywords are `user=`, `password=`, `message=`,
`tag=`, `log=` and `clt_conn_tag=`, and TTL is static on the
`external_acl_type` line — so under any caching scheme a console decision with a
specific expiry could not be expressed to Squid, and Squid's `ttl=` would only
ever approximate it as a polling interval. With no cache the question is moot:
the console applies its own time-boxing on every query, and revocation takes
effect on the next request rather than one TTL later.

**`negative_ttl=0` is the half that matters most.** A stale allow is the obvious
hazard, but a stale _deny_ is the one that gets felt: operator approves, agent
retries, and a cached `ERR` refuses it for another 30 seconds — which reads as a
broken approval flow at precisely the moment someone is watching it.

The cost is one in-cluster RPC per gated request. That is affordable **only
because the static allowlist short-circuits first**: the hot path — LLM API,
Forgejo, GitHub — never reaches the helper, so the RPC is paid on exactly the
rare, interesting traffic the console wanted to see anyway. Without that
ordering, `ttl=0` would put a console round-trip in front of every request the
fence handles.

Consequences to build for:

- **The helper must be genuinely concurrent.** Request rate now equals helper
  call rate for undecided domains. An asyncio helper with `concurrency=64` is
  fine; fork-per-request would serialise the fence.
- **Queue overflow stops being theoretical.** `queue-size` defaults to
  `2*children-max`, and with nothing cached every gated request is a live helper
  call. Whatever Squid does when that queue fills has to land on the deny side,
  so it needs establishing rather than assuming — same spike, same run.
- **The helper must call the console's in-cluster Service, not
  `haku.allegedly.works`.** Every gated request pays this hop, so it should not
  traverse ingress, public DNS or the internet, and a fail-closed gate should not
  depend on public routing being healthy. The static allow of
  `haku.allegedly.works` is a different path — that one is for the _agent's_ own
  console traffic.

#### Console downtime degrades reach, not operation

There is no cache to soften an outage, so fail-closed is now absolute for gated
hosts: console down means every _undecided_ domain is denied, immediately.

What keeps that from being a fence-wide outage is the static allowlist. With the
console down, an agent keeps talking to everything already sanctioned in git and
loses only the ability to reach somewhere **new**. That is a good failure
profile, and it has a maintenance implication: **it holds only while the static
list is genuinely comprehensive.** A thin static list silently converts a console
outage into an agent outage.

#### The circular dependency to avoid

Haku reaches the console at `haku.allegedly.works` **through this proxy**. If the
gate can deny that host, then a console outage — or a bad policy entry — leaves
the agent unable to reach the console that would fix it.

**Decided (operator, 2026-08-10): `haku.allegedly.works` is statically allowed,
ahead of the gate.**

The exception costs nothing, because the egress fence was never what protected
the console: it authenticates its own callers, and an agent reaching it can only
do what its bearer already permits under the console's own approval policy. This
is the same reasoning that justifies the existing `toEntities: cluster` carve-out
in the haku CCNPs — per `haku/docs/security.md`, in-cluster services authenticate
their own callers, so reachability is not where that boundary is drawn.

Keep the static set minimal and justified by that test: a host belongs there only
if something other than the fence is already authorising access to it.

#### Return `ERR`, not `BH`, when the console is unreachable

Squid documents `BH` as "an internal error occurred in the helper" but does not
specify how it treats `BH` versus `ERR` operationally. Since the requirement is
a deterministic deny, the helper should return `ERR` for both "console said no"
and "console unreachable", and distinguish them with `log=` so the audit can
tell a policy decision from an outage. Do not rely on `BH` semantics for a
security outcome.

#### Use `message=` to tell the agent why

`message=` is surfaced in the error response, so a denial can carry
`message="pending operator approval"` rather than an opaque 403. That turns
deny-now/allow-on-retry into something an agent can act on deliberately —
retry later versus give up — instead of guessing.

#### Agent-scoped decisions make the cache-key question go away

An earlier subsection frames `%>a %>rd` keying as a problem because a new
sandbox pod re-prompts. With the console as a durable policy store that is no
longer true: a cache miss costs an **RPC, not a human interaction**. The console
answers a known (agent, host) pair instantly from its table.

What does need solving is that `%>a` is a pod IP, which is ephemeral, while
"agent-scoped" implies a stable identity. The helper should map IP → pod → a
stable owner label via the Kubernetes API and send _that_ to the console, so
decisions survive pod churn.

### ICAP: substitution can be a service call, not generated config

**`external_acl_type` cannot rewrite headers.** Its response keywords are a
closed set — `user=`, `password=`, `message=`, `tag=`, `log=`, `clt_conn_tag=` —
and `user=`/`password=` feed `cache_peer login=`, i.e. upstream _proxy_ auth, not
origin credentials. `url_rewrite_program` only touches URLs. So the gate helper
cannot also do substitution.

**ICAP REQMOD can** (RFC 3507), as can eCAP. A REQMOD service receives the
request after `ssl_bump` decrypts it and returns a _modified_ request: arbitrary
header rewriting, in any language. It can also block, which means **one ICAP
service could do policy and substitution in a single hop** against haku-console,
with `icap_service … bypass=0` making adaptation failures fatal — the fail-closed
posture this note already requires. That is a far more direct expression of "the
console governs policy" than generating Squid config.

**Alpine's squid has neither.** Confirmed from the image's own configure line:
`--enable-openssl --enable-ssl-crtd …` and no adaptation flags. Debian enables
`--enable-icap-client` and `--enable-ecap` in its shared build flags and ships a
`squid-openssl` flavour adding `--with-openssl --enable-ssl-crtd`, so the spike
image moved to `debian:testing` (#4025). 7.6 exists only in forky/sid; trixie is
on 6.13.

**What it changes for the topology question.** Without ICAP, per-client
substitution in a _shared_ Squid could only be static ACLs keyed on client —
agents × credentials, regenerated on every agent churn. That was the strongest
argument for keeping credentials out of a shared instance. ICAP removes it, so
"credentials stay in iron" becomes a choice rather than a constraint, and the
shared-Squid option is live on much better terms.

**Run 3, 2026-08-15 — the base swap cost nothing.** Debian `squid-openssl` 7.6,
uid 13 (`--with-default-user=proxy`, where Alpine's was 31 — the Deployment had
to follow, #4027). Clean start, zero restarts, and all four substitution cases
behave exactly as on Alpine: placeholder → real credential, the
other-destination placeholder passed through **unmodified**, an unrelated bearer
untouched, and base64 `Basic` rewritten. So `ssl_bump`, certgen and the
destination-scoped rules are unaffected by musl → glibc.

ICAP itself is compiled in but **not yet exercised** — no `icap_service` is
configured, and nothing has been adapted. That is the next thing to try, not a
thing that works.

### Direction: ICAP only, with haku-console as the ICAP endpoint

Operator, 2026-08-15: probably **just ICAP**, if it works — and rather than a
helper process in the Squid pod, **haku-console exposes the ICAP endpoints
itself**. No `external_acl_type` gate, no separate substitution service. Squid
points `icap_service` at the console; one REQMOD call decides policy and rewrites
the credential.

**The win is that no credential lives in the proxy at all.** No `envsubst`, no
tmpfs render, no per-proxy SOPS secrets, no generated per-agent credential
config. The secret exists in console, transits Squid per request, and is never at
rest there. That does not answer "which fence holds which credential" — the
question this note opens with — it deletes it. It also drains most of the heat
from per-agent-vs-shared, since the thing being partitioned is no longer in the
proxy.

**The static allowlist survives without `external_acl`.** It is a plain
`acl known dstdomain "/etc/squid/allowed-domains.txt"` — config, no helper, no
round trip. Dropping the ACL helper costs nothing there, and `adaptation_access`
can keep statically-allowed, uncredentialed traffic away from console entirely.

#### The trade: console moves onto the critical path

This inverts a property decided earlier and should be a decision, not a surprise.

The gate design deliberately degraded gracefully: console down meant agents kept
reaching everything already sanctioned in git and lost only _new_ domains.
Downtime cost reach, not operation.

If the credential comes from console, that reverses. The credentialed hosts **are
the hot path** — LLM API, Forgejo, GitHub — so console down means no LLM access
at all. `adaptation_access` cannot rescue it: it can skip ICAP for
allowed-and-uncredentialed hosts, but the credentialed ones are exactly the ones
that must go through.

Defensible, since fail-closed already made console a hard dependency for
undecided domains. But it is a strictly stronger coupling than what "console
downtime degrades reach, not operation" described, and it argues for console HA in
a way the gate-only design did not.

#### Two practical unknowns

- **REQMOD hands the service the whole request, body included.** For a POST to
  the LLM API that means every prompt transiting console — volume and sensitivity
  that may not be wanted. ICAP `Preview` plus `204 No Content` exists for this,
  and header-only rewriting ought to answer off the preview, but how that
  interacts with returning a _modified_ message needs verifying rather than
  assuming. **Verified 2026-08-15: it does not help — Squid ignored the stub's
  `Preview: 0` offer and encapsulated the full body. See step 1 results below.**
- **Console needs a real ICAP server**, not a REST endpoint: encapsulated HTTP
  messages, the `Encapsulated` header, `OPTIONS`, `204`, chunked bodies.
  `pyicap` exists. Bounded work, but a niche protocol, and nothing in the
  existing FastMCP surface helps.

#### Test it in three steps, not one

The tempting move is to wire the console endpoints and drive them with an
experimental agent. That couples three unknowns at once — whether Squid's REQMOD
does what we need, whether the console implementation is right, and whether the
agent path works — so a failure anywhere reads as a failure everywhere.

1. **Stub ICAP service in the spike namespace.** Not console: a small service
   that logs what it receives and rewrites one header. It answers every
   Squid-side question below at the cost of no console code, and the contract it
   establishes transfers unchanged. If REQMOD cannot do what is needed, nothing
   has been written twice.
2. **Console's ICAP endpoints**, driven by the same spike Squid and the existing
   `curl` harness. One new variable.
3. **An experimental agent**, end to end. One more.

What step 1 has to answer, alongside the fail-closed cases already listed:

- Does REQMOD see the **bumped plaintext** request? Expected — adaptation runs
  post-decryption — but unobserved.
- Can it rewrite `Authorization`, and can it **block**?
- Does `bypass=0` fail closed on service-down _and_ on timeout?
- What arrives for a POST: full body, or preview?
- Does `http_access` run before adaptation? Still worth knowing without
  `external_acl`, because it decides whether the static allowlist can keep denied
  traffic away from console.

#### Step 1 results (measured 2026-08-15, Squid 7.6)

Stub service in <../../k8s/x/squid-egress-spike/app/icap_stub.py>, driven by
`curl` through the spike Squid against the header-echoing origin. Every row below
was observed, not inferred. The stub reflects its own view back as `X-Icap-Saw-*`
request headers, because the ICAP pod's log is unreadable from an agent sandbox —
`pods/log` is refused in `squid-egress-spike` and the namespace is not in the
`loki-read-proxy` allowlist.

| question                                    | answer                                                                                                       |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| REQMOD sees the bumped plaintext            | yes — rewrote `Authorization`, origin received the injected value                                            |
| REQMOD can block                            | yes — encapsulated `403` reached the client with the service's own body                                      |
| `204 No Content`                            | yes — request forwarded unmodified                                                                           |
| POST body                                   | transits in full, intact, `Content-Length` preserved                                                         |
| ICAP closes mid-transaction, `bypass=0`     | fail-closed: `500`, `X-Squid-Error: ERR_ICAP_FAILURE 0`, origin never contacted                              |
| ICAP hangs, `bypass=0`, **default timeout** | **not** fail-closed — Squid waited 45s and applied the adaptation                                            |
| ICAP hangs, `icap_io_timeout 5 seconds`     | fail-closed: same `ERR_ICAP_FAILURE`, at **10.2s** — Squid retries once, so deny latency is _2×_ the timeout |
| recovery after a failure                    | immediate — the next request succeeded in every case                                                         |

Three findings that change the design rather than confirm it:

**REQMOD runs _before_ Squid's own header rewriting, so the ICAP service is not in
the secret path.** The decisive observation: with the client sending
`Bearer spike-bearer-placeholder`, the service saw a 31-character value (the
placeholder) and never the 34-character substituted secret, and neither
`X-Spike-Client` nor `Via` — both added by Squid — were present at adaptation
time. Substitution still fires afterwards: the same placeholder sent in
`passthrough` mode arrived at the origin as `Bearer fake-real-bearer-do-not-use`.

This is the answer that opens the shape of step 2. **haku-console _can_ be handed
ICAP endpoints without being handed the credentials** — it would see and rule on
the agent's placeholder, and `credentials.conf` would redeem it downstream, out of
console's sight. The `option A` objection at the top of this document ("an ICAP
service that now holds the credentials") is not forced by REQMOD-for-policy; it
binds only if substitution itself moves into the service. That makes it a choice
rather than a consequence — see the decision below.

**Squid ignored a `Preview: 0` offer and sent the whole body anyway.** The stub
advertises `Preview: 0` in its `OPTIONS`; Squid encapsulated `req-body` and
delivered all 26 bytes with no preview and no `100 Continue` round trip. So the
first practical unknown above resolves the pessimistic way, and for a blunter
reason than expected: not "preview can't coexist with a modified response" but
"Squid did not use preview at all". Every LLM prompt would transit console. If
that volume is unwanted, the lever is `adaptation_access` — scope REQMOD to the
requests whose policy actually needs deciding — not `Preview`.

**Caller identity arrives out-of-band and trustworthy.** `icap_send_client_ip on`
puts the real client address in the ICAP `X-Client-IP` header (`10.244.8.246`,
the probe pod), and it arrives _before_ the `request_header_add "%>a"` trick this
document proposed for the `external_acl` design. Console reads `X-Client-IP` and
needs nothing stamped into the HTTP request at all.
`icap_send_client_username` sends nothing absent proxy auth, as expected.

Two consequences for config, both now in <../../k8s/x/squid-egress-spike/app/squid.conf>:

- **`icap_io_timeout` must be set explicitly.** Its default is `read_timeout`,
  15 minutes, so an unresponsive console hangs the agent instead of denying it —
  the opposite of the stated requirement that expiry _is_ a denial. Size it at
  half the budget, since the observed deny takes two timeout periods.
- **`icap_service_failure_limit`** (default 10) marks the service down after that
  many failures and, with `bypass=0`, fails everything until Squid retries. Right
  direction for a fence; it does mean a flapping console takes egress hard-down
  rather than degrading, which argues for console HA in the same direction the
  critical-path section above does.

Still unanswered from the step-1 list: whether `http_access` runs before
adaptation. The spike is `http_access allow all`, so denied traffic never existed
to observe; answering it needs a deny rule, and it only matters as an optimisation
(keeping already-denied traffic away from console), not for correctness.

#### Decided: console holds the substituted credentials (operator, 2026-08-15)

Step 1 turned "console is in the secret path" from a consequence into a choice.
Taking it deliberately: **console does policy _and_ substitution.** The
placeholder-only variant — console rules, `credentials.conf` redeems — is not
what we are building.

This is the version this section originally described, and it keeps the win that
motivated it: no credential lives in the proxy at all. No `envsubst`, no tmpfs
render, no per-proxy SOPS secrets, no generated per-agent credential config. The
secret exists in console, transits Squid per request, and is never at rest there.
"Which fence holds which credential" is deleted rather than answered, and
per-agent-vs-shared stops being a credential-partitioning question.

What is accepted along with it, all of it measured rather than assumed:

- **Console sees live credentials**, by design. It is the credential authority
  now, not merely a policy oracle. The operator's earlier judgement that Squid
  can be trusted on the secret path (2026-08-10) extends to console here.
- **Every credentialed request body transits console.** Squid ignored the
  `Preview: 0` offer and encapsulated the full body, so for the LLM API that is
  every prompt. `adaptation_access` scoping is the only lever, and it cannot be
  applied to the credentialed hosts — they are exactly the ones that must go
  through.
- **Console down means no LLM access**, not merely no new domains. The
  critical-path inversion above is now the operating reality rather than a trade
  under consideration, and `icap_service_failure_limit` makes a _flapping_
  console hard-down rather than degraded. Console HA is a prerequisite for this
  design carrying production agent traffic, not a later refinement.

`credentials.conf`, `envsubst`, and the fake-credential ConfigMap stay in the
spike: they are what the placeholder-redemption half is measured against, and
step 1 used them to prove the ordering. They do not survive into the real fence.

### Direction: one Squid per _agent_, provisioned by haku-console

Refinement of "one Squid per fence" (operator, 2026-08-10): the console manages a
Squid **per agent**, so each agent gets its own fence — its own allowlist, its own
credentials, its own policy.

**Identity stops being a lookup and becomes a constant.** If the proxy instance
_is_ the agent, the helper does not need `%>a` at all: the agent id is baked into
that Squid's own config (a helper argument, or `%DATA` on the `acl … external`
line). Unforgeable for the right reason — the agent cannot edit the config of the
proxy it is fenced by. This removes the IP → pod → owner-label mapping through the
Kubernetes API, which was the fiddliest piece of the design, and it downgrades
verification item 4 below from load-bearing to nice-to-know.

**Credential blast radius shrinks to one agent.** Today
`haku-openclaw-spike-proxy` holds five secrets on behalf of one spike; per-agent
proxies mean a compromised proxy exposes exactly the credentials of the agent it
serves.

**It must be a separate pod, not a sidecar.** Tempting to co-locate it in the
agent's pod for lifecycle reasons, but Cilium enforces network policy **per
endpoint, i.e. per pod** — it cannot distinguish the agent container's traffic
from the sidecar's within one pod. The whole fence today rests on the sandbox pod
being allowed to reach _only_ the proxy pod, while the proxy pod holds the wider
egress. Collapsing them into one pod gives the agent container the proxy's egress
rights directly, and the fence becomes advisory. Sidecar is the one shape that
silently undoes this.

**The cost is cache fragmentation**, taken to its limit: every agent starts cold.
That is the same trade already accepted when per-fence `emptyDir` beat a shared
cache, pushed one step further — worth noting only because caching was
requirement 1, and per-agent isolation is close to the worst case for it. If hit
rate ever matters, that is the axis that pays for it.

**Config becomes runtime-generated rather than GitOps'd**, since the console
writes each agent's allowlist and credential rules at provision time. That is a
departure from the cluster's declarative norm, but a precedented one — agent
sandboxes are already provisioned dynamically rather than committed. Worth being
deliberate about, not accidental. The outage-resilience property survives
relocation: config is materialised at pod start, so a console outage cannot
retract a running agent's static allowlist — it only blocks provisioning new
agents.

#### Provisioned sandboxes must inherit their caller's fence

`haku-sandbox-mcp` lets an agent provision a sandbox. If the new sandbox is
fenced by anything other than **the calling agent's own proxy**, then
`provision_sandbox` _is_ the fence escape: the agent never has to defeat its
proxy, it just asks for a box behind a different one. Per-agent fences make this
sharp — differing fences is the entire point, so "which fence does a provisioned
box get" stops being a detail.

Requirement: a sandbox provisioned by agent A egresses only through A's Squid,
and inherits A's allowlist and credentials.

Three things that has to rest on:

- **Attribution must come from the authenticated caller, never a parameter.** The
  MCP server has to know _which agent_ called from the credential on the call. If
  agents share one bearer for `haku-sandbox-mcp`, this is unenforceable — so
  per-agent MCP credentials are a prerequisite, not a later refinement.
- **The fence is the NetworkPolicy, not the proxy env var.** `HTTP_PROXY` in a
  sandbox is a _hint_: the workload can unset it. Only a CNP restricting that
  pod's egress to its agent's Squid makes it a fence. Setting the env and calling
  the box proxied would be exactly the advisory-fence mistake.
- **Fence membership should be a label the workload cannot change.** A per-agent
  CNP selecting `agent=<id>` and permitting egress only to that agent's Squid
  makes any correctly-labelled pod fenced by construction. That holds only while
  sandbox pods lack RBAC to patch their own labels — worth asserting explicitly,
  since relabelling would be a fence change.

A pleasant consequence: if the agent's Squid is torn down, its sandboxes lose
egress rather than falling back to open. Lifetime coupling in the fail-closed
direction.

### Verify before building: what only running answers

Everything below is a Squid behaviour that reading cannot settle, ordered by how
much it would hurt to discover late. All of it can be answered by one stub
helper — no haku-console integration needed. Choose the behaviour **by the
hostname requested** (extra `Service` names aliasing the echo origin, so they
resolve under the CNP's `**.cluster.local` DNS rule): `allow-origin` → `OK`,
`deny-origin` → `ERR`, `slow-origin` → sleep past the client timeout,
`crash-origin` → `BH`, `dead-origin` → helper exits mid-query.

1. **Does it fail closed, deterministically?** Load-bearing: if Squid cannot be
   made to deny reliably on helper failure, the gate belongs somewhere else and
   this design changes shape. Four cases — `ERR`, `BH`, helper hangs, helper dies
   — must all end denied, and the log should say which is which. This also
   settles whether returning `ERR` for both "console said no" and "console
   unreachable" was the right call, or merely a cautious one.
2. **Is the helper called once per request, or twice?** A bumped connection has
   two decision points: the `CONNECT` (host from the CONNECT line) and the inner
   request after decryption (full URL). If Squid consults the helper at both,
   console load doubles and one fetch may raise two prompts. This sizes the "one
   RPC per gated request" claim made above.
3. **Does `ttl=0` actually disable the result cache?** See the UNVERIFIED note.
   Two identical requests, count helper invocations. If `ttl=0` does not do it,
   try `cache=0`; if neither does, the no-cache decision needs another mechanism.
4. **Is `%>a` the client pod's IP?** The 2026-08-10 run could not show this —
   `X-Spike-Client` read `127.0.0.1` because curl ran inside the Squid pod. Drive
   it from a **second pod** and confirm the source survives without SNAT to a
   node IP. **Downgraded** by the per-agent direction above: if the proxy
   instance is the agent, identity is a constant in that Squid's config and `%>a`
   only separates pods _within_ one agent's fence — useful for audit, not for the
   decision. Still worth knowing, no longer load-bearing.
5. **Does `message=` reach the agent?** Through a bumped tunnel the error page is
   generated inside the TLS session. If it arrives, deny-now/allow-on-retry
   becomes actionable ("pending operator approval") instead of an opaque 403.

Queue overflow (`queue-size`, default `2*children-max`) falls out of the same run
if the slow host is driven concurrently.

**A design fork sits behind (4).** With one Squid per fence, the fence _is_ the
agent identity, and `%>a` only separates pods within a fence. If that is
sufficient, the IP → pod → owner-label mapping through the Kubernetes API — the
fiddliest part of this design — is unnecessary.

### The simpler alternative, if the live hook is too much

haku-console could instead **own `allowed-domains.txt`**, writing approved
domains into a ConfigMap that Squid reloads. No request-time helper, no
blocking, no cache-key question. The cost is latency between approval and
effect, and losing per-request context in the prompt. Worth considering as
step one, with the live helper as step two.

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
