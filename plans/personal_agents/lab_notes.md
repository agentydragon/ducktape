# Personal Agents — Lab Notes

Running log of experiments in the `agent-lab` namespace against
<success_criteria.md>. Records what was tried, what happened, and the rough
edges — including the hypotheses that died.

**Rig — prefer `k3d` over the real cluster.** A full k3s cluster runs inside the
agent container in ~30 seconds (`k3d cluster create`), so anything structural —
CRD shapes, operator behaviour, admission, policy, whole third-party stacks — is
to be tested there rather than against production, and without time-boxed RBAC
grants. `kind` does **not** work here (F12: cgroup v1 vs its systemd node image).
Docker works too, which is what made the OpenShell-on-Docker run in F13 possible.
Only use the live cluster when the thing under test genuinely depends on it —
Cilium, Authentik, Flux, the real LiteLLM — and say which of those it needs.

**Gotcha:** `k3d cluster create` merges into the default kubeconfig **and switches
the current context**, so the next unqualified `kubectl` silently hits the toy
cluster. Restore with `kubectl config use-context <prod>` and pass `--context`.

**Live-cluster rig** (when actually required): `agent-lab` namespace (time-boxed
grant, #3557 + #3558). Agents are driven from inside the gateway pod:

```bash
kubectl exec -n agent-lab <pod> -c openclaw -- \
  openclaw agent --agent <id> --session-key <key> --json -m "<prompt>"
```

## Configurations under test

| Tag         | Shape                                                            | Egress boundary                 |
| ----------- | ---------------------------------------------------------------- | ------------------------------- |
| `oc-lab`    | Operator-managed OpenClaw + OpenShell sandbox (`mode: mirror`)   | OpenShell policy (sandbox only) |
| `oc-solo`   | Operator-managed OpenClaw, `sandbox.mode: "off"` — one container | Attempted: allowlist proxy      |
| `lab-proxy` | mitmproxy in `agent-lab`, CONNECT-host allowlist, own CA         | n/a (it _is_ the boundary)      |
| `oc-iron`   | OpenClaw holding a placeholder, behind iron-proxy                | NetworkPolicy + iron-proxy      |
| `oc-trust`  | OpenClaw with the CA in the system store, zero CA env vars       | NetworkPolicy + `lab-proxy`     |

## Results

### `oc-lab` — operator + OpenShell split execution

| Criterion                 | Status       | Evidence                                                     |
| ------------------------- | ------------ | ------------------------------------------------------------ |
| S1 agent stood up         | **pass**     | Completed an `exec` round-trip in its own sandbox            |
| S2 PR end to end          | **fail**     | Task-level attempt died mid-run when the sandbox wedged (F1) |
| S2 multi-turn repo work   | partial pass | Small repo: clone → verify → commit → survived turn boundary |
| S3 memory across sessions | not run      | Abandoned once the exec path proved unstable                 |
| S4 no arbitrary Internet  | **fail**     | Confinement is process-scoped and bypassable (F2, F3)        |

### `oc-plain` — no operator, one container, allowlist proxy ⇒ **meets every hard requirement**

| Criterion                 | Status              | Evidence                                                                                                                                       |
| ------------------------- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| S1 agent stood up         | **pass**            | `hostname; echo SOLO_OK` returned from inside the harness pod                                                                                  |
| S2 PR end to end          | **pass**            | [#3560](https://github.com/agentydragon/ducktape/pull/3560), opened end to end from one task-level instruction                                 |
| S3 memory across sessions | **pass**            | Full acceptance: told "remember this: …8-1-4-9", wrote `MEMORY.md` itself, **pod deleted**, fresh session answered `8149` with zero tool calls |
| S4 no arbitrary Internet  | **pass**            | Full probe matrix below                                                                                                                        |
| S5 whole harness confined | **pass (the want)** | One container behind one boundary — B2's acceptable topology                                                                                   |

S4 probe matrix, run inside the harness container:

```text
A. proxy stripped, example.com    -> fail
B. proxy stripped, api.github.com -> fail    (cannot bypass even to an allowed host)
C. direct IP 1.1.1.1              -> fail
D. via proxy example.com          -> blocked (domain allowlist)
E. via proxy api.github.com       -> 200     (allowed domain works end to end)
```

B is the important line: egress is impossible except through the proxy, so the
allowlist is enforced rather than advisory. This is the first configuration to
satisfy S4.

### `public-coder-agent` — the same shape, in production ⇒ **all four hard requirements met**

The lab shape promoted to `cluster/k8s/agents/public-coder-agent/`, Authentik-gated
and restricted to `agentydragon`. Differences from `oc-plain` that matter: the
proxy's CA is issued by cert-manager and distributed by trust-manager instead of
being generated by mitmproxy (F8), and egress destination enforcement is a Cilium
`toFQDNs` policy on the proxy pod rather than a mitmproxy addon.

| Criterion                 | Status              | Evidence                                                                                                                                            |
| ------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| S1 agent stood up         | **pass**            | Serving on 18789 behind the Authentik outpost; 0 restarts over 3h                                                                                   |
| S2 PR end to end          | **unblocked**       | The one blocker is fixed and deployed (#3577). Verified live: `env \| grep -c GH_PAT` -> 2, `api.github.com/user` -> 200 `login=agentydragon-agent` |
| S3 memory across sessions | **pass**            | Same harness, same `MEMORY.md` mechanism, verified under pod deletion                                                                               |
| S4 no arbitrary Internet  | **pass**            | Matrix below, with **TLS verification working** — no `-k` needed                                                                                    |
| S5 whole harness confined | **pass (the want)** | One container behind one boundary                                                                                                                   |

Production test-drive, 2026-07-30 — one task-level instruction, no assistance:

```text
instruction: open a PR against agentydragon/ducktape targeting devel, ...
result:      23 tool calls; edited the file, committed locally (46c8766a)
             fatal: could not read Username for 'https://github.com'
             "this environment has no GitHub push credentials configured"
```

The agent did everything up to the push and then reported the blocker plainly
instead of working around it — it did not go looking in `/proc` for the token,
though F7 shows it could have. Causation is not inferred:

```text
container env via kubectl exec:  GITHUB_TOKEN: set   GH_PAT: (unset)
the agent's own exec tool:       env | grep -c GITHUB_TOKEN -> 0
                                 tok:(empty)  pat:(empty)
```

So F7's denylist strip is the **sole** remaining blocker on S4-confined
production, and the one-line rename is both necessary (shown here) and
sufficient (shown in the lab, where the same harness opened #3572). Production
cannot pass S2 until that commit merges; the manifest carries it already, and it
cannot be applied live because the debug RBAC grant is read-only
(`cannot patch resource "deployments"`), which is the correct scope for it.

S4 probe matrix, run inside the production harness container:

```text
curl api.github.com, verification ON  -> 200        (trust chain intact)
git ls-remote, verification ON        -> OK
example.com via proxy                 -> blocked
direct 443 github.com, proxy stripped -> blocked
direct 443 raw IP 1.1.1.1             -> blocked
kube-apiserver 10.96.0.1:443          -> blocked    (no lateral movement)
litellm :4000                         -> 200
raw DNS to 8.8.8.8                    -> blocked    (after the kube-dns narrowing)
```

The last line was the one outstanding S4 defect and is now closed. An unscoped
port-53 rule is a tunnel the proxy allowlist never sees; scoping the rule to
`k8s-app: kube-dns` in `kube-system` blocks 8.8.8.8 while leaving in-cluster
resolution, the proxy, and LiteLLM working — verified in the lab before shipping,
then in production.

The `kube-apiserver` line is worth keeping: it shows the fence is not just
"no Internet" but "no cluster either", which matters because the agent's pod has
a service account token on disk.

### `oc-iron` — the credential never in the agent, off-the-shelf proxy ⇒ **passes**

The `oc-plain` shape with the hand-written mitmproxy addon swapped for
[iron-proxy](https://github.com/ironsh/iron-proxy) in `replace` mode: the agent
holds `GH_PAT=proxy-github-placeholder` and the real PAT lives only in the
proxy. The disposable lab manifest has been retired; its deployed successors are
the [public coder agent](../../cluster/k8s/agents/public-coder-agent) and the
[Haku OpenClaw spike](../../cluster/k8s/agents/haku-openclaw-spike). Detail: F15,
F16.

| Criterion                   | Status   | Evidence                                                                                         |
| --------------------------- | -------- | ------------------------------------------------------------------------------------------------ |
| S2 PR end to end            | **pass** | `agentydragon-agent/ducktape#3`, 7 tool calls, no insecure flags; proxy log `POST /pulls -> 201` |
| S4 no arbitrary Internet    | **pass** | Probes run **by the agent itself**, not via `kubectl exec`                                       |
| Credential not in the agent | **pass** | 0 real-token matches in env and across `/proc/*/environ`; still `login=agentydragon-agent`       |
| git transport               | **pass** | `ls-remote` and a **3.9 MB** push, both authenticating via Basic-auth substitution               |

S4 matrix, as the agent reported it:

```text
https://example.com/        -> 000   rejected at CONNECT, by allowlist
https://en.wikipedia.org/   -> 000   same
https://api.github.com/user -> 200   login: agentydragon-agent
direct, proxy env stripped  -> blocked by NetworkPolicy
```

Two worries did not survive: the 1 MiB `max_request_body_bytes` default does not
apply to the git transport, and Basic-auth substitution works with real `git`
rather than only synthetic `curl`. What stands is fail-open on a missing secret,
and the trust cost of a third-party binary in the credential path.

**The manifest was then redeployed verbatim** — iron-proxy plus the system trust
store, a combination neither earlier run had exercised — and it was broken (F18).
After the fix, every client verifies with no CA variable but Node's:

```text
CA env vars besides NODE_EXTRA_CA_CERTS: 0
curl + placeholder                     -> http=200
git ls-remote (GnuTLS + Basic swap)    -> 50d1b3c9  HEAD
node https                             -> status 200
python3 urllib                         -> 200
example.com                            -> http=000
real token anywhere in the agent env   -> 0
```

### `oc-trust` — the CA in the system store, and **zero** CA environment variables

Built to test the fix for F17. Same image, same intercepting proxy, the CA
appended to `/etc/ssl/certs/ca-certificates.crt` by an initContainer:

```text
CA env vars set in the pod: 0

git ls-remote  (GnuTLS)    -> TLS verified
curl           (OpenSSL)   -> 200
python3 urllib (OpenSSL)   -> 200
node https     (own roots) -> UNABLE_TO_VERIFY_LEAF_SIGNATURE
example.com                -> 000   confinement intact
```

So `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO` and `REQUESTS_CA_BUNDLE`
can all be deleted, leaving `NODE_EXTRA_CA_CERTS`. This removes the class of bug
F17 describes rather than working around it — there is no longer a variable name
for the harness denylist to catch.

**Rig gotcha:** `agent-lab`'s LimitRange defaults containers to 512Mi, and
OpenClaw is OOMKilled with exit 137 the moment it does real work. Set resources
explicitly; production's 768Mi/4Gi is fine.

### `public-coder-agent` after the cutover — the credential is out of the agent

The lab shape shipped to production (#3582): iron-proxy 0.49.0 in `replace` mode
holding the PAT, the agent holding `GH_PAT=proxy-github-placeholder`, the CA in
the system trust store, and the destination allowlist deliberately off.

| Check                       | Result                                                                        |
| --------------------------- | ----------------------------------------------------------------------------- |
| Credential possession       | **pass** — 0 real-token matches in env and across `/proc/*/environ`           |
| Placeholder authenticates   | **pass** — `api.github.com/user` returns `login: agentydragon-agent`          |
| TLS with one CA variable    | **pass** — curl 200, git OK, node 200, only `NODE_EXTRA_CA_CERTS` set         |
| Egress open, credential not | **pass** — `example.com` 200, `en.wikipedia.org` 301, neither given the token |
| Proxy is the only route out | **pass** — direct egress with the proxy variables stripped still fails        |
| S2 end to end               | **pass** — 12 tool calls, no insecure flags, PR opened on its own fork        |

**The push is the part that had never been proven in production**, and the proxy
audit log shows the whole handshake — including that a public read needs no
credential, so an earlier `git ls-remote` proved nothing about substitution:

```text
GET  /agentydragon-agent/ducktape.git/info/refs        200  -                     (public read)
POST /agentydragon-agent/ducktape.git/git-upload-pack  200  -                     (public fetch)
GET  /agentydragon-agent/ducktape.git/info/refs        401  -                     (push challenged)
GET  /agentydragon-agent/ducktape.git/info/refs        200  header:Authorization  (git retries with Basic)
POST /agentydragon-agent/ducktape.git/git-receive-pack 200  header:Authorization  (the push itself)
POST /repos/agentydragon-agent/ducktape/pulls          201  header:Authorization  (the PR)
```

That is the 401-challenge → retry-with-Basic → substitute sequence, on the real
git transport, against real GitHub. Four requests in the whole run carried the
credential, all of them GitHub, none of them `example.com` or `wikipedia.org` —
so wide egress and a scoped credential coexist exactly as intended.

**The agent was told the contract in chat, not in config**: that `$GH_PAT` is a
placeholder to use as it would a real token. It did, first try, with no prompting
about proxies or certificates.

## Findings

Numbered in discovery order and cited by number from cluster manifests, so the IDs
are stable. Full index and text: [findings/](findings/README.md).
