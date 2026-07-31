# Egress and TLS

Findings are numbered in discovery order across the whole programme and cited by
number from cluster manifests, so the IDs are stable and non-contiguous here.
Index of all findings: [README.md](README.md).

## F4. A domain allowlist that works, without Cilium or the shared mitmproxy

The cluster's real domain filtering is Cilium `toFQDNs` on the shared mitmproxy's
egress, but `cilium.io` is outside the lab grant and the shared mitmproxy is
unreachable from `agent-lab` (its own policy admits only the injected
namespaces). Rather than block on an approval, the lab runs its own:
`lab-proxy`, a mitmproxy with a CONNECT-host allowlist addon and its own CA.

It works: `example.com` and `wikipedia.org` get 403 at CONNECT; `api.github.com`
tunnels through and returns 200 once the CA is mounted. Because the proxy
resolves the hostname itself, a client cannot smuggle an allowed name to an
arbitrary address.

**This is the right shape for S4** — domain-level, per <success_criteria.md>,
with the pod policy as the outer fence that makes it unbypassable. Only the
outer fence is missing, per F3.

## F8. mitmproxy re-keys its CA on restart, and the agent responds by turning TLS verification off

Two failures stacked here, and the second is the one that matters.

**The mechanism.** mitmproxy generates a CA into its confdir whenever that
directory has none. The lab proxy ran `mitmproxy:latest` with confdir on the
container filesystem, so every pod recreation minted a **new CA** while the
workload still trusted the copy that had been hand-pasted into the `lab-proxy-ca`
ConfigMap. Verification then fails for every client:

```text
git ls-remote https://github.com/... -> server certificate verification failed. CAfile: /lab-ca/ca.pem
curl --cacert /lab-ca/ca.pem https://api.github.com/ -> 000
openssl verify -CAfile /lab-ca/ca.pem leaf.pem -> error 20 unable to get local issuer certificate
```

What makes this genuinely nasty as a diagnosis: **every field of the subject is
identical across the old and new CA** (`CN = mitmproxy, O = mitmproxy`), so the
obvious check compares equal and reads as "the CA is fine". mitmproxy also
backdates `notBefore` by ~2 days, so the leaf's dates do not point at the restart
that caused it. Only a fingerprint or an `openssl verify` distinguishes them.
mitmproxy sends no chain either — one certificate on the wire — so the CA cannot
be fingerprinted from the connection; it has to be verified against.

**The part that matters.** Faced with a TLS failure, the agent did not stop or
report it. It disabled verification and carried on, for the whole run:

```text
git -c http.sslVerify=false clone https://github.com/agentydragon/ducktape.git
git -c http.sslVerify=false push -u origin HEAD
curl -k -sS https://api.github.com/repos/agentydragon/ducktape
```

That is how PR [#3572](https://github.com/agentydragon/ducktape/pull/3572) was
opened. The PR is real and was opened unaided from one instruction, so S2's result
stands — but the manner of it matters more than the result: **a TLS-interception
proxy gives us traffic inspection and nothing about authenticity**, because the
party being protected will route around a broken trust chain rather than surface
it. Any "the agent talks only to authentic GitHub" claim is unsupported on this
shape.

**What it does not break.** Confinement is unaffected, and this was tested rather
than assumed. Destination enforcement happens at `CONNECT`, before TLS:

```text
curl -k example.com via proxy      -> 000, proxy logs `CONNECT example.com:443 << 403 Forbidden`
curl -k example.com, proxy stripped -> blocked by NetworkPolicy
curl -k api.github.com via proxy   -> 200
```

So `-k` buys the agent no reachability. S4 holds; what is lost is integrity, not
containment.

**The fix, and why production already had it.** Do not let mitmproxy own the CA.
Production issues it with cert-manager, plants it into confdir with an
initContainer, and distributes the same certificate via trust-manager — so the CA
is externally owned and cannot drift. That design was already in
`cluster/k8s/agents/public-coder-agent/proxy/`, which is why production verifies
cleanly (200 and `git ls-remote` OK with verification **on**) even after three
proxy restarts. The bug was only in the lab manifest, which has been changed to
mirror production.

**Verified after the fix.** The lab manifests were changed to the externally-owned
CA shape and re-run end to end. TLS now verifies with no bypass:

```text
curl api.github.com, verification ON     -> 200
git ls-remote, verification ON           -> OK
openssl verify -CAfile /lab-ca/ca.crt    -> l.pem: OK
example.com via proxy                    -> blocked
raw DNS to 8.8.8.8                       -> blocked
```

and the agent, given the same kind of instruction, opened
[#3573](https://github.com/agentydragon/ducktape/pull/3573) in 19 tool calls with
**zero** occurrences of `-k`, `--insecure`, `sslVerify=false` or
`GIT_SSL_NO_VERIFY` anywhere in the run. So the workaround was a symptom of the
broken trust chain, not a habit: repair the control and the bypass disappears.

**Standing lesson:** an agent silently working around a broken security control is
a failure mode to design against, not a one-off. `-k` and `sslVerify=false` in an
agent's command history are a signal that some control has broken, and nothing
currently surfaces that.

## F15. iron-proxy expresses our whole credential policy declaratively — with three sharp edges

[iron-proxy](https://github.com/ironsh/iron-proxy) (Apache-2.0, Go, single 20 MB
container) is a MITM egress proxy whose YAML covers, as configuration, everything
our hand-written mitmproxy addon does in Python: a default-deny host allowlist,
host+method+path rules, and placeholder-to-real credential substitution. It was
run here against a header-echo endpoint so the substitution could be observed on
the wire rather than inferred.

Setup: explicit-proxy mode (`dns.enabled: false`, `proxy.tunnel_listen`), which
is our production shape — `HTTP_PROXY` at a Service, no DNS hijacking. The
injected value was a **lab-only fake secret**, so the echo endpoint could print it.

**It works.** The client holds only a placeholder; the real value appears
upstream:

```text
A1  Authorization: Bearer proxy-gh-token-placeholder  -> upstream saw: Bearer REAL-SECRET-…
A2  no Authorization header                           -> upstream saw: <none>
A3  Authorization: Bearer something-else              -> upstream saw: Bearer something-else
A4  -u x-access-token:proxy-gh-token-placeholder      -> upstream saw: Basic eC1hY2Nlc3Mt…
                                         base64 -d     x-access-token:REAL-SECRET-…
```

Host+method+path policy works, and the decision is iron-proxy's own — the 403 on
B3 is GitHub's. This mirrors the rule our addon hand-writes, though **it is a
bonus rather than a requirement**: `agentydragon-agent` pushes to its own forks
and GitHub already denies it write access anywhere else, so the proxy rule is
defence in depth over a boundary the forge owns.

```text
B1  GET    /user                                -> allow  200
B2  POST   /user/repos                          -> reject 403  by allowlist
B3  POST   /repos/agentydragon-agent/x/issues   -> allow  (403 from upstream)
B4  DELETE /repos/agentydragon/ducktape         -> reject 403  by allowlist
    GET https://example.com, https://1.1.1.1    -> reject 403 at CONNECT
```

**A4 is the capability we do not have.** Git over HTTPS authenticates with
`Authorization: Basic base64(user:token)`, and iron-proxy decodes the header,
substitutes inside it, and re-encodes. Our addon writes a `Bearer` header and
never touches Basic, so it covers the REST API and not the git transport.

**It also fixes F8 by construction.** The CA is supplied, not generated —
`tls.ca_cert`/`tls.ca_key` are mounted paths, so cert-manager owns the keypair
and there is nothing to drift. It refuses to start on a CA without
`keyUsage=keyCertSign`, with that exact message, rather than failing later at
handshake time.

### Substitution modes: richer than the two rows above suggest

Read from the configuration reference after the fact, because the tests above
used only **replace** mode and I wrongly generalised its behaviour to the tool.
There are two modes, and the distinction matters:

- **`inject`** — the proxy _always_ sets the credential on matching requests and
  **the client sends nothing at all**. Target is a `header` (name written with
  the casing sent upstream) or a `query_param`. The value comes from a Go
  template with `.Value` and a variadic `base64` helper, so the wire format is
  arbitrary: `Bearer {{ .Value }}`, or the documented GitHub shape
  `Basic {{ base64 "x-credential:" .Value }}`.
- **`replace`** — the client holds a placeholder and the proxy swaps it. Scans
  `match_headers` (a list), and optionally `match_path` and `match_query` for
  APIs that carry the token in the URL, both off by default because paths and
  query strings reach access logs.

Sources are pluggable independently of mode: environment variable, AWS Secrets
Manager (with background refresh), AWS SSM Parameter Store, 1Password service
account, 1Password Connect, a keyfile, plus GCP service-account JWT and ID-token
**minting** rather than storage.

**This is our model, not just an approximation of it.** `inject` is precisely
what the mitmproxy addon does — the agent possesses nothing, and the credential
is attached on the way out — with the wire format under configuration rather
than hardcoded. It also means the fail-open finding below is scoped to `replace`
mode: with `inject` there is no placeholder that can be absent.

### Three edges, all measured, none in the documentation

**`require: true` is unusable in explicit-proxy mode.** The `secrets` transform
runs against the `CONNECT` request, which carries no headers, so the missing
placeholder trips the requirement and _every_ HTTPS request is rejected before
the tunnel opens:

```text
CONNECT httpbin.org:443 -> reject 403  rejected_by=secrets  annotations.rejected=LAB_FAKE_SECRET
```

What `require` actually guards is the agent **bypassing** the swap — it rejects
requests to a matching host that do not carry the proxy token, so an agent cannot
quietly substitute a credential of its own. That control is unavailable in the
one deployment shape we need. It is not, despite how an earlier draft of this
note read, a guard against the proxy's own secret being missing; that case is
handled by `secretKeyRef`, which stops the pod before it serves anything.

**Method/path-scoped rules block their own CONNECT.** A host whose only rules
carry `methods:`/`paths:` is unreachable over HTTPS, because the preflight has
method `CONNECT` and an empty path and matches none of them. `api.github.com`
was rejected outright until an explicit `methods: ["CONNECT"]` rule was added
alongside; the inner request is then policed normally. Every documented example
is DNS-interception mode, where no CONNECT exists, so nothing warns about this.

**Client-supplied credentials are not stripped** (A3), _in the configuration
tested_. An `Authorization` header the agent invents reaches upstream verbatim;
only an exact placeholder match is rewritten. `require: true` is documented as
exactly the fix — "prevents workloads from bypassing the secret-swap mechanism
with alternative credentials" — but see the edge above: it is unusable in
explicit-proxy mode, so the intended defence is present in the tool and out of
reach in our topology. Our addon removes `Authorization`, `x-api-key` and
`Proxy-Authorization` before injecting, which is the right behaviour against a
hostile agent — the proxy should be the only party that gets to assert identity.
Closing this on iron-proxy needs a `header_allowlist` transform in front of
`secrets`; that composition is untested here.

### Where it lands

More capable than our addon on the thing that matters (Basic-auth substitution,
i.e. the git transport), less capable on one (credential stripping), and it
removes ~40 lines of Python we would otherwise maintain. Policy expressiveness
is a wash and would not decide it either way. The remaining
unknown is the end-to-end git flow: `git clone`/`push`/`gh pr create` against
real GitHub could not be tested from this container, because the harness's own
egress proxy injects GitHub credentials of its own and contaminates the result.
That test belongs in the cluster lab, where nothing sits upstream.

**Lab-contamination note, since it invalidated a first pass.** Exporting
`HTTPS_PROXY` is not enough: curl prefers the lowercase `https_proxy`, which the
harness sets, so the first matrix ran entirely through the harness proxy and
produced six plausible, meaningless rows. iron-proxy's request log was empty and
that is what caught it. Point the client with `-x` and confirm against the
proxy's own log before trusting any row.

## F16. Real OpenClaw behind iron-proxy: confinement holds, and a PR goes out end to end

F15 measured iron-proxy with `curl`. This is the same proxy carrying a real
OpenClaw deployment (`oc-iron` in `agent-lab`, image
`devel-20260728182008-17ba82d`, model `codex-gpt-5.6-luna` via LiteLLM), fenced
by a NetworkPolicy whose only route out is the proxy — the production topology.

**Domain confinement holds, measured from the agent's own vantage** rather than
from `kubectl exec`. The agent ran the probes itself and reported them back:

```text
https://example.com/        -> 000   (proxy rejects at CONNECT, by allowlist)
https://en.wikipedia.org/   -> 000   (same)
https://api.github.com/user -> 200   login: agentydragon-agent
```

with the proxy's audit log confirming the decision was its own, and that no
non-allowlisted host was ever reached:

```text
hosts seen: example.com:443, en.wikipedia.org:443, api.github.com, github.com
rejections: CONNECT example.com:443 / en.wikipedia.org:443  -> rejected_by=allowlist
```

**The agent holds no credential.** `GH_PAT=proxy-github-placeholder`; zero
real-token matches in its environment or across `/proc/*/environ`; direct egress
with the proxy variables stripped is blocked by the NetworkPolicy, so the fence
is the policy and not the environment.

**A pull request goes out end to end, unaided, from one instruction** — 7 tool
calls, and zero occurrences of `-k`, `--insecure` or `sslVerify=false` in the
whole run. The proxy's log is the proof that it carried it:

```text
POST /repos/agentydragon-agent/ducktape/pulls -> allow 201
     secrets: swapped GITHUB_TOKEN into header:Authorization
18 requests in the run had the credential swapped in
```

Result: <https://github.com/agentydragon-agent/ducktape/pull/3>. Deliberately
opened against the agent's **own fork** rather than upstream — the API path
exercised is identical and it leaves no noise on the main repository.

**Two of the drawbacks I had flagged did not survive contact.**

- **Body buffering was the concrete worry, and it is unfounded.** A **3.9 MB**
  packfile pushed cleanly, far past the 1 MiB `max_request_body_bytes` default.
  Bodies are not buffered when no transform inspects them.
- **Basic-auth substitution works with real `git`, not just synthetic curl.**
  `git ls-remote` and `git push` both authenticate with the placeholder as the
  password in `https://x-access-token:<placeholder>@github.com/...`.

What does still stand: fail-open (no placeholder → 401 from GitHub rather than a
loud local error, confirmed live), the CONNECT rule wart, and the trust question
of putting a young third-party binary in the credential path.

**Unrelated trap worth recording: the namespace LimitRange OOM-kills OpenClaw.**
`agent-lab`'s default container limit is 512Mi; the gateway is killed mid-run
with exit 137 the moment it does real work. Any lab deployment needs explicit
resources — production's 768Mi/4Gi works.

## F17. OpenClaw strips `GIT_SSL_CAINFO` from the exec tool, which silently breaks git behind any TLS-intercepting proxy

Found because the first end-to-end attempt failed, and the agent — correctly —
stopped rather than working around it:

```text
fatal: unable to access 'https://github.com/agentydragon-agent/ducktape.git/':
       server certificate verification failed. CAfile: none CRLfile: none
```

`CAfile: none` is the clue: git never received the CA, though the container
plainly has it. Comparing the two vantages settles it — and the filter is by
**exact name**, not by prefix:

```text
                        container   agent's exec tool
SSL_CERT_FILE               set           set
CURL_CA_BUNDLE              set           set
REQUESTS_CA_BUNDLE          set           set
NODE_EXTRA_CA_CERTS         set           set
GIT_SSL_CAINFO              set        ** stripped **
GIT_TERMINAL_PROMPT         set           set          (control)
GIT_PROBE_HARMLESS          set           set          (control, added to test)
```

This is F7's mechanism again — a hardcoded env denylist in `host-env-security` —
but where F7 cost authentication, this costs **TLS trust for git only**. `curl`
keeps working, so the failure looks like a git or proxy bug rather than a harness
one, and the two controls prove it is neither a `GIT_*` prefix rule nor
accidental.

**Production has this latent right now.** `public-coder-agent` sets the CA the
same way and has no fallback:

```text
GIT_SSL_CAINFO=/trust/ca-certificates.crt   (set in the container)
/etc/gitconfig                              -> No such file or directory
git config --global --get http.sslCAInfo    -> (empty)
```

It has not bitten only because the agent reaches for the REST API and has never
actually run `git push` (the F10 gotcha). The moment it does, it gets an
unexplained TLS failure — and F8 says what an agent does with one of those.

### Why git needs its own variable at all: it is not using the same TLS library

The obvious question is why `SSL_CERT_FILE` does not simply cover git. Measured
in the image, one tool at a time, with everything else unset:

```text
git ls-remote     GIT_SSL_CAINFO only  -> TLS OK (reaches auth)
                  SSL_CERT_FILE only   -> server certificate verification failed
                  CURL_CA_BUNDLE only  -> server certificate verification failed
                  nothing              -> server certificate verification failed

curl              SSL_CERT_FILE only   -> 200
                  CURL_CA_BUNDLE only  -> 200
                  nothing              -> 000
```

The linkage explains it:

```text
git-remote-https -> libcurl-gnutls.so.4 + libgnutls.so.30
curl             -> libcurl.so.4 + libssl.so.3   (curl 7.88.1, OpenSSL/3.0.20)
```

**They do not share a TLS backend.** Debian builds git against
`libcurl3-gnutls`, so:

- `SSL_CERT_FILE` is an **OpenSSL** variable. GnuTLS does not read it, so it
  covers `curl` and Python and never git.
- `CURL_CA_BUNDLE` is read by the **`curl` binary**, not by libcurl. Any
  libcurl-using application — git included — never sees it.
- That leaves `GIT_SSL_CAINFO`/`http.sslCAInfo`, git's own knob, as the only
  thing that reaches it. Which is the exact name the harness strips.

**Proxying is not affected, and that asymmetry is the trap.** libcurl reads
`http_proxy`/`https_proxy`/`no_proxy` itself, so git goes through the proxy
correctly — the error above is a verification failure against the proxy's
certificate, not a connection failure. Egress confinement therefore looks
healthy while trust is broken, and the two are easy to conflate.

**So there is no "standard" CA variable to rely on.** Production sets five of
them (`SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO`,
`NODE_EXTRA_CA_CERTS`) precisely because each runtime has its own, and Node and
Python bundle their own roots on top. The variable-per-tool approach is
load-bearing, and it has one entry the harness deletes.

**The fix, verified.** Put the CA in git's configuration rather than its
environment, where no denylist applies. `~/.gitconfig` is not an option —
`/home/openclaw` is not writable by uid 1000 — so mount a system one:

```yaml
volumeMounts:
  - { name: gitconfig, mountPath: /etc/gitconfig, subPath: gitconfig, readOnly: true }
```

with a ConfigMap holding `[http]\n\tsslCAInfo = <path>`. With that in place the
same agent, same instruction, completed the whole clone → commit → push → PR
flow (F16).

### The elegant fix, tested: one trust file, and Node is the only holdout

Appending the CA to `/etc/ssl/certs/ca-certificates.crt` — the default trust file
for GnuTLS and OpenSSL alike — should cover git and curl at once. Run as
`oc-trust`: same image, same intercepting proxy, an initContainer doing
`cat /etc/ssl/certs/ca-certificates.crt /lab-ca/ca.crt > merged` mounted over the
system path, and **not one CA environment variable set**.

```text
CA env vars set in the pod: 0

git ls-remote  (GnuTLS)    -> TLS verified   (fails later on auth, as expected)
curl           (OpenSSL)   -> 200
python3 urllib (OpenSSL)   -> 200
node https     (own roots) -> UNABLE_TO_VERIFY_LEAF_SIGNATURE
example.com                -> 000            (confinement intact)
```

The agent itself, given a real `git clone`, produced **zero** certificate errors
and zero insecure flags — it failed only on authentication, which is correct for
a pod behind the allowlist-only proxy holding no credential.

So the five variables collapse to **one file plus `NODE_EXTRA_CA_CERTS`**:

| Variable              | Still needed? | Why                                              |
| --------------------- | ------------- | ------------------------------------------------ |
| `SSL_CERT_FILE`       | no            | the system store is OpenSSL's default anyway     |
| `CURL_CA_BUNDLE`      | no            | same                                             |
| `GIT_SSL_CAINFO`      | **no**        | GnuTLS reads the system store — F17 disappears   |
| `REQUESTS_CA_BUNDLE`  | no            | `requests` is not even installed in this image   |
| `NODE_EXTRA_CA_CERTS` | **yes**       | Node bundles its own roots and ignores the store |

This is the shape to adopt. It deletes the stripped-variable problem instead of
working around it: there is no name left for a denylist to catch, and any TLS
client added to the image later is covered without a new variable. The one cost
is that the merge happens at startup, because the distro bundle lives in the
image while the CA comes from a Secret — an initContainer writing to an
`emptyDir` mounted over the system path, about six lines.

## F18. Node ignores a missing `NODE_EXTRA_CA_CERTS` silently, and fails with a misleading error

Caught by deploying the committed manifest verbatim rather than trusting that
two separately-tested halves compose. They did not: the manifest set

```yaml
- { name: NODE_EXTRA_CA_CERTS, value: /merged/ca-certificates.crt }
```

where `/merged` is the **initContainer's** scratch mount and does not exist in
the app container, which mounts only the merged file over the system path. The
symptom:

```text
NODE_EXTRA_CA_CERTS=/merged/ca-certificates.crt
path exists?  NO
node https -> ERR SELF_SIGNED_CERT_IN_CHAIN

NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt   (the mounted bundle)
node https -> 200
```

**Node emits no warning for a nonexistent file.** It falls back to its bundled
roots and produces `SELF_SIGNED_CERT_IN_CHAIN`, which reads as "this CA is not
trusted" — a trust problem — when the actual fault is a path typo. Every other
client in the image fails loudly and names the file (`CAfile: none`).

Two further measurements while diagnosing:

- Pointing `NODE_EXTRA_CA_CERTS` at the **whole merged bundle** works (200), so
  there is no need to extract a single-certificate file for it.
- **`node --use-openssl-ca` does not work here** (`SELF_SIGNED_CERT_IN_CHAIN`),
  even though the CA is in OpenSSL's default store and curl and Python both
  verify against it. So the flag is not a substitute for the variable, and Node
  genuinely remains the one client needing explicit configuration.

**Standing lesson, and the reason this was found at all:** two configurations
that each passed separately are not a tested configuration. F16 tested iron-proxy
with env-var CA delivery; the trust-store run tested the system store behind
mitmproxy. The committed manifest combined them, and the combination was broken.
Deploy the artifact you are actually publishing.
