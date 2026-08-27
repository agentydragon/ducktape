# Personal Agents — TODO

## Rotate the `agentydragon-agent` GitHub PAT — do this first

The first 20 characters of the token were printed into a Claude Code session
transcript on 2026-07-29. The cause was mine and was avoidable: a check meant to
report only whether the variable was set used

```bash
echo "GITHUB_TOKEN set: ${GITHUB_TOKEN:+yes}${GITHUB_TOKEN:-NO}"
```

`${VAR:-default}` expands to **the value** when the variable is set, so the
second half printed the secret. Only `${VAR:+yes}` is safe for a presence check;
`:-` must never appear in one.

A 20-character prefix is not directly usable, and the transcript is not public.
Rotate anyway rather than reason about how much of a credential is enough — the
token can push to repositories and open pull requests as `agentydragon-agent`.

Rotation touches the ExternalSecret source, not the manifests: the value comes
from `ClusterSecretStore/kubernetes-claude-sandbox-secret-store`, key
`github-token`, property `token`, consumed by
<../../cluster/k8s/agents/public-coder-agent/app/github-token-eso.yaml> and by
the OpenShell `agentydragon-github` provider. Both pick up a new value on the
next refresh (1h) with no manifest change.

**Delete this section once rotated.**

## Decide deliberately how the iron-proxy image is pinned

The adoption decision asked for digest pinning plus a mirror
(<../../cluster/docs/container-images.md>), and the answer moved underneath the
question. The Docker Hub half is settled: production no longer runs
`ironsh/iron-proxy:0.49.0` but a commit-built image of our own
(<../../cluster/images/iron-proxy/>, upstream commit `c90f4fe`) from
`git.allegedly.works`, so no third-party account stands between an attacker and the
one process holding the GitHub credential.

What remains is that the image now tracks a **moving tag** under Flux image
automation, on that same process — see
<../../docs/personal_agents/README.md> § "Rough edges". Defensible because
the build is ours, but it should be a decision rather than a side effect of how the
temporary fork was packaged. Fold it into the return to the official image that
<../../cluster/k8s/agents/public-coder-agent/README.md> already schedules for
iron-proxy v0.50.0.

## Cost a Kubernetes `WorkerProvider` — the live candidate for W2

The one hard-ish want still unmet is execution off the harness container, and the
survey found a path to it that nothing else here records:
<../../docs/personal_agents/verdicts.md> § "Still open". OpenClaw's **cloud
workers** feature
already does git-backed workspace sync, and does it properly — staged result refs,
three-way merge against the dispatch-time manifest, conflict handling — where the
OpenShell mirror does a whole-tree destructive replace on yield. `WorkerProvider`
is a public plugin-SDK type; only the bundled `crabbox` implementation is
cloud-VM-shaped, not the interface.

It also inverts credential placement the right way: the box authors git history
credential-free and the gateway owns push/PR, so no standing forge credential
lives where the agent runs. That would improve `public-coder-agent` too, not just
a future personal-data agent.

Cost it before assuming W2 is blocked on OpenClaw's maturity: the answer we have
is "the OpenShell plugin is under-tested", not "OpenClaw cannot split execution".
And note we have no working split-execution deployment at all right now:
`public-coder-agent` runs `sandbox.mode: "off"`, and the main `openclaw` gateway
— the one thing configured for the OpenShell mirror — is unused and believed
broken. So W2 is unattempted, not tried-and-failed, and the mirror's weaknesses
are an argument from upstream code rather than from our own operational
experience.
Three unknowns to settle first — how much of the provider contract assumes a VM,
whether a pod can satisfy the setup/lease lifecycle, and where sandboxing (H3)
comes from once OpenShell is out of the path.

## Decide the orphaned `kagent.dev` CRDs

`agentharnesses.kagent.dev` and friends are still installed with no controller —
the `kagent` namespace runs no pods and there is no HelmRelease. A CRD with no
controller is inert YAML that reads like an available capability.

Either remove them or write down why they are kept. `AgentHarness` is the one
worth a deliberate decision rather than a sweep: it is the right shape for what we
want (see <../../docs/personal_agents/verdicts.md> § Control planes), and
keeping the CRD costs
nothing if the reason is recorded. Right now it is neither kept nor cleaned.

## Use k3d, not production RBAC, for the next round of experiments

**Standing preference, not a suggestion: experiment in `k3d` whenever it can
answer the question, and use the real cluster only when it cannot.** `k3d` runs a
real k3s cluster inside the agent container in about 30 seconds (F12), so
structural work — CRD shapes, operator behaviour, admission, policy, whole
third-party stacks — no longer needs time-boxed grants against production. Docker
works in the container too, which is what let F13 stand up an entire OpenShell
Docker-driver stack locally.

When reaching for the live cluster anyway, name the dependency that forces it —
Cilium, Authentik, Flux, the real LiteLLM — otherwise use k3d. `kind` does
**not** work here: the host is cgroup v1 and kind's node image boots systemd,
which cannot mount its own cgroup hierarchy nested.

Two things to remember: `k3d cluster create` switches the default kubeconfig
context, so pass `--context` explicitly or the next `kubectl` hits the toy
cluster; and the local cluster has none of the real cluster's Cilium, Authentik
or Flux wiring, so it answers "does this API work" and not "does this work here".

## Try Docker-in-Kubernetes if S5 becomes worth its cost

The path exists and is evidenced (F12), it is just expensive. The gateway has a
Docker driver selectable with `OPENSHELL_DRIVERS=docker` — verified initialising
in a container — and that driver is the one that honours
`OPENSHELL_SANDBOX_COMMAND`, so the harness would run inside the sandbox.

Shape: a second OpenShell gateway with `OPENSHELL_DRIVERS=docker`, alongside a
Docker daemon in the pod.

What it costs: a privileged Docker-in-Docker pod, a second gateway to operate, and
stepping outside the operator's declarative model — against a shape whose only
gain over today's `public-coder-agent` is S5, a _want_ rather than a hard
requirement. Do not start this unless S5 becomes a real requirement.

**Now proven, mostly** (F13): the whole stack was built locally and a sandbox on
the community OpenClaw image reached `SANDBOX_PHASE_READY`, with
`openclaw-start` launching OpenClaw's gateway inside it. Driven entirely over gRPC
with `grpcurl` and the repo's protos, since the `openshell` CLI cannot be fetched
here.

What that exposed as the real work, beyond the DinD pod itself: gateway JWT auth
is mandatory for docker sandboxes; the supervisor binary must be extracted and
passed as `supervisor_bin`; every path the gateway hands Docker must be identical
inside and outside its container; sandbox callbacks need `host_gateway_ip` and
host networking. And setting the startup command still means recreating the
sandbox container out of band — exec is not a substitute, because a backgrounded
gateway dies with its exec session.

Still untested: whether a privileged DinD pod is admitted in our cluster at all.

## Revisit OpenShell for S5 only if the k8s operator gains an entrypoint _and_ ingress

Tested and closed for now — full evidence in
<../../docs/personal_agents/findings/openshell.md> F11.

NemoClaw genuinely runs the harness inside a sandbox, so the shape is real. It
works because the Docker driver persists `OPENSHELL_SANDBOX_COMMAND` (NemoClaw
sets it to `nemoclaw-start`, which launches `openclaw gateway run` as a separate
`gateway` uid so the sandboxed agent cannot tamper with it). The Kubernetes
operator does not offer that, and three measured blockers stand:

1. `spec.environment` refuses the knob by name — "keys starting with `OPENSHELL_`
   are reserved".
2. The image entrypoint is discarded; the pod runs
   `/opt/openshell/bin/openshell-sandbox` and the community image's
   `openclaw-start` never executes.
3. A sandbox cannot be reached: no ports, no Service, ingress only from the
   gateway pod on TCP/2222, while our gateway must answer Authentik on 18789.

Upstream agrees this is unbuilt rather than hidden — `openshell-k8s-operator` at
HEAD says "selection, entrypoint, and TTL/cleanup arrive in later milestones".

(1) and (2) are one milestone away. (3) is the deeper one and nothing suggests it
is planned, because a sandbox is modelled as something you exec into rather than
something that serves. Do not re-attempt until both land.

## Move the agent to a GitHub App installation token

The forge, not the proxy, is where write scope belongs. Today the agent's account
already cannot write outside its own forks, so the fork constraint holds — but it
holds by virtue of what `agentydragon-agent` is, not by anything we declare, and
the PAT itself is still push-anywhere-that-account-can-reach.

A GitHub App installation token is scoped by GitHub to specific repositories and
permissions and expires in an hour; a fine-grained PAT gets most of the way for
much less work. Either makes the scope a reviewable fact about the credential
rather than a property of a proxy config, so a proxy bug degrades to "the token's
own permissions". Installation tokens also retire the rotation item at the top of
this file, because they rotate themselves.

Strongest remaining move on the credential axis, and not started. Prefer this
over adding request-level rules to the proxy.

## Verify OpenClaw configs against the shipped schema **in CI**

Two config values on `public-coder-agent` were accepted by the file and rejected
at gateway startup, one deploy cycle each:

- `bind: "all"` — not in `{auto, lan, loopback, custom, tailnet}`
- `mode: off` unquoted in YAML — parsed as boolean `false`

Both are schema-detectable. `openclaw config schema` emits the full JSON schema
(~2.5 MB) and constrains exactly these fields; `openclaw config validate` checks
the active config without starting the gateway. A hermetic test — commit the
schema alongside the image tag it came from, validate both OpenClaw configs
against it with `jsonschema` — kills the class. `kubeconform` cannot, because the
config is opaque JSON inside a ConfigMap.

Until this exists, shape verification is a manual step and therefore skippable --
see the rough edge in <../../docs/personal_agents/findings/rough_edges.md>.
The point of the TODO is to stop relying on remembering.

Scope: `cluster/k8s/agents/public-coder-agent/app/openclaw.json`. The retired
operator-managed gateway also embedded config in a CRD field, but that second
configuration no longer exists.

Caveat to handle deliberately: a committed schema drifts from the image. Record the
source image tag next to it and regenerate when the image pin moves. Prefer that
over a `requires_docker` test that dumps the schema live -- correctness is the same
and the hermetic version runs in ordinary CI.

## Cheaper test runs

Context-window probes must hit the exact model whose limit is being declared, so
model choice is not the lever there — probe count and size are. The GLM
measurement spent ~5M input tokens bisecting a 1M-wide range to 2,000-token
precision, then rounded the result to 1,000,000 anyway.

- Confirm a candidate value with one probe instead of hunting the boundary;
  bisect only when the boundary itself is the deliverable.
- Probe only the models actually declared. Three of the first six `codex-*`
  searches were for models later dropped from the config.
- `--calibrate` on a 1,000-token payload first. It costs nothing and catches both
  measurement traps (prompt caching, filler token density) before they corrupt an
  expensive run.

Everything that is _not_ a context probe — exec round-trips, credential presence,
memory persistence, first-contact bootstrap — should run on a cheap model. All of
those were run on `codex-gpt-5.6-luna` for no benefit; they assert on shell
output, not model quality.

`openai_utils/probe_context_window.py` currently pushes the expensive shape: its
`--precision` default is 2,000 and nothing suggests confirming a candidate.

## Measure the remaining GLM models

Only `glm-5.2-anthropic` is measured (accepted 1,037,527 tokens; declared
1,000,000). The other seven GLM entries carry 1,000,000 **by assumption**, and
`maxTokens` (96,000) is unmeasured for all of them. Limits are per-route, so 5.2's
number is not evidence for the rest.

Above ~1.04M the 5.2 route returns HTTP 200 with `input_tokens: 0`. The probe's
truncation guard reports that as TRUNCATED rather than scoring it a pass; the true
ceiling is unknown, not 1,037,527.

## Detect an agent routing around a broken security control

The agent hit a broken TLS trust chain and silently switched to
`curl -k` / `git -c http.sslVerify=false` for the rest of the run rather than
reporting it (F8). Nothing surfaced that. Containment held — destinations are
enforced at `CONNECT`, before TLS — so this is an integrity and observability gap,
not an escape.

Cheap detector, since every request already transits an intercepting proxy —
iron-proxy for `public-coder-agent`, mitmproxy for the Haku zones. The proxy sees
which client connections skip verification, and `-k`/`sslVerify=false` appearing in
an agent's command history is a reliable signal that some control has broken. Worth
alerting on rather than discovering by reading transcripts weeks later.

## Give the OpenClaw agents their own Langfuse project, or populate the key alias

Traces do land (F20) — but every agent's traffic goes into one
`langfuse-litellm-project`, and `user_api_key_alias` is **not populated**, so the
only way to tell whose request a trace is is the model name. That worked here
only because `gemini-embedding-2` is unique to OpenClaw's memory search; it stops
working the moment two agents share a model, which is already nearly true for the
Codex lane.

Either fix removes the guesswork: a separate Langfuse project per agent (cleaner
separation, and per-agent retention/limits), or getting LiteLLM to carry the
virtual key's alias into the trace metadata so a filter is a lookup rather than an
inference. The alias is already the natural key — the virtual keys are per-agent
and named.

Do this before adding a second agent on the same models, not after.

## Make Langfuse’s read path survivable

F20: `langfuse-web` aborted (exit 134, SIGABRT under a 2Gi limit) twice during a
single investigation, both times serving an ordinary read — `limit=25` over three
hours was enough. The API materialises full request and response bodies and these
traces carry whole `CLAUDE.md` payloads, so cost scales with window × limit.
Ingestion was never affected; that is `langfuse-worker`, a different pod.

So the observability data is fine and the tool for looking at it is not, which is
worth separating when judging whether Langfuse is "working". Options, unranked:
raise the web tier's memory, cap or paginate on the client side, or use the
aggregate endpoints (`/api/public/metrics/daily` returned a full day in 1.4s
against a query that killed the pod).

Until then: aggregates for counts, `limit<=5` and a tight window for bodies.

## Verify iron-proxy's `oauth_token` transform before designing around it

[personal_data_agent.md](personal_data_agent.md) puts iron-proxy's `oauth_token`
transform (`grant: refresh_token`) at the centre of the personal-data agent's
Google credential path, and every claim about it so far is read off the shipped
example config rather than measured. Four things to test in the lab, in this
order — each one invalidates the design if it fails:

1. **A real refresh-token exchange through the proxy.** One Google client, one
   read-only scope, `curl` from the agent side with no credential at all, and a
   200 back from `gmail.googleapis.com`.
2. **`require: true` against a CONNECT.** The same flag on the `secrets`
   transform rejected every HTTPS request in tunnel mode (F15). The docs say this
   one is per-entry with 502 semantics, which is a different code path — but that
   is exactly the assumption F15 punished.
3. **`paths:` scoping on an `oauth_token` rule**, and whether it needs a paired
   `methods: ["CONNECT"]` entry the way allowlist rules do. Needed because
   Calendar and Drive can both arrive on `www.googleapis.com`, so host rules may
   not separate them.
4. **Two holders of one refresh token.** Airlock refreshing on its 300s loop
   while iron-proxy also exchanges the same token. Google is not supposed to
   rotate refresh tokens on use; if it does, iron-proxy's env copy goes stale
   between the Secret write and the reloader restart.

## Stop mirroring a live Google access token into the sandboxes

`cluster/k8s/agents/airlock/google-access-token-eso.yaml` puts a working Google
access token into `claude-sandbox` and `haku-sandbox` on a 1-minute refresh, where
an agent can read it. That is the same exposure class as F7/F10 — a credential
readable from inside the agent, and so reachable by prompt injection from anything
the agent reads — and it is the one the coder agent's proxy design exists to
remove.

Not urgent in itself: the grant is nine read-only scopes, so the blast radius is
disclosure rather than modification. But it should not be the pattern the
personal-data agent inherits, and the replacement (proxy-held or MCP-held) is
already designed. Fold it in when that agent is built, and retire the mirror
rather than adding a third namespace to it.

## Ask whether Airlock's `google` provider should stay a union grant

Its single provider entry requests nine read-only scopes across Gmail, Drive,
Calendar, Tasks, Contacts, Docs, Sheets, Slides and YouTube. haku-console went the
other way — separate Google Cloud projects and clients for Mail and Calendar,
explicitly so their verification status and credential lifecycles stay
independent, which is what stops Gmail's Testing-mode 7-day refresh expiry from
dragging Calendar down with it.

The union grant is fine while nothing consumes it. It stops being fine the moment
a second consumer wants a subset, because there is no way to give it one.
