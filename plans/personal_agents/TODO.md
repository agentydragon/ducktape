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

## Ship the credential-injecting proxy to `public-coder-agent`

**No longer blocked on OpenShell.** F10 has this working in the lab: the token
lives in the proxy, the agent container has none (env _and_ `/proc` clean), and
the agent still opened PR #3574 unaided. It also carries a write policy confining
writes to the agent's own fork — defence in depth rather than the mechanism,
since GitHub already denies `agentydragon-agent` write access outside its own
forks. Do not let that rule gate the rollout.

Production already runs the mitmproxy this needs, with no addon wired, so the
change is a ConfigMap, a volume, `-s`, moving `GITHUB_TOKEN` from the agent
Deployment to the proxy Deployment, and widening the Cilium `toFQDNs` policy not
at all. Do **not** add `--ignore-hosts`; ignored hosts skip the addon hooks and the
injection never runs.

Order this **after** the `GH_PAT` rename merges, not instead of it: the rename is
already proven and unblocks S2 immediately, and this supersedes it cleanly (with
injection the agent needs no token variable at all, under any name).

Two things to carry over from the lab:

- Match policy paths against **both** the REST API shape
  (`/repos/<owner>/<repo>/...`) and the git transport shape
  (`/<owner>/<repo>.git/...`). The agent uses the API and never ran `git push`; a
  git-shaped policy blocks its entire workflow while looking correct.
- Let Airlock own token rotation and hand the value to the **proxy**. Airlock
  distributes credentials to consumers as Secrets, which is refresh, not
  possession — pointing it at the proxy instead of the agent composes the two
  properly.

## Fix `GIT_SSL_CAINFO` in production before the agent ever runs `git push`

F17: OpenClaw's exec tool strips `GIT_SSL_CAINFO` by name, so git behind the
TLS-intercepting proxy fails with `CAfile: none` while `curl` keeps working.
`public-coder-agent` sets the CA that way and has neither `/etc/gitconfig` nor a
global `http.sslCAInfo`, so it is one `git push` away from an unexplained TLS
failure — and F8 records what an agent does with one of those.

**Do it by putting the CA in the system trust store, not by patching gitconfig.**
Both fixes are verified (F17). The `/etc/gitconfig` one works and is narrow; the
better one puts the bundle at `/etc/ssl/certs/ca-certificates.crt`, which covers
git, curl and Python at once and lets `SSL_CERT_FILE`, `CURL_CA_BUNDLE`,
`GIT_SSL_CAINFO` and `REQUESTS_CA_BUNDLE` all be deleted. Only
`NODE_EXTRA_CA_CERTS` stays, because Node ignores the system store — and it must
point at the **mounted** path, since Node ignores a missing file silently (F18).

**In production this needs no initContainer.** The lab merged the distro bundle
with the CA at startup; production's trust-manager `Bundle` already carries
`useDefaultCAs: true` plus the cluster root plus the proxy's interception root,
under the key `ca-certificates.crt`. So the whole change is a `subPath` mount of
a ConfigMap already in the namespace, over the system path, plus deleting four
environment variables.

That turns a stripped-variable workaround into the removal of the whole class:
there is no longer an env var name for the harness denylist to catch. It is also
the one change here that is **strictly** an improvement — it fixes a live latent
bug, removes configuration, and adds no dependency and no new failure mode.

Cheap, and it removes a trap rather than adding a feature.

## Decide on iron-proxy — the case is now narrow and testable

F15 and F16 measured it, including a real OpenClaw opening a PR end to end
behind it. Two of the three drawbacks did not survive: the 1 MiB body-buffer
worry (a 3.9 MB push went through) and unproven Basic-auth substitution (real
`git ls-remote` and `git push` both work with the placeholder).

What remains is one property and one judgement call:

- **No anti-bypass control.** `require: true` would reject requests to GitHub
  that do not carry the placeholder — stopping an agent from substituting a
  credential of its own — and it is unusable here because it is evaluated
  against the header-less `CONNECT`. Low severity: an agent that brings its own
  token already had it.
- **The placeholder contract has to reach the agent**, since `replace` only acts
  on requests carrying it: reach GitHub with `$GH_PAT`, a placeholder whose real
  value is attached on the way out. Saying it in the OpenClaw chat at cutover is
  enough — it does not have to be committed anywhere to be tried.
- **Trust.** It puts a ~540-star, roughly year-old Go binary from a vendor with
  a commercial product into the credential path, replacing mitmproxy, which is
  mature and already runs here. If adopted: pin by digest and mirror to Harbor
  rather than tracking `ironsh/iron-proxy:latest`.

Client-supplied auth headers are **not** stripped, but that is out of scope: an
agent that brings its own token already had it, and the property we need is that
it never holds ours.

Not a slam dunk either way. The honest position is that our addon works, is
smaller, and rests on a better-established engine; iron-proxy is a real upgrade
on configurability and on the git transport. Reach for it when the addon becomes
a maintenance burden, not before.

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

Tested and closed for now — full evidence in [lab_notes.md](lab_notes.md) F11.

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

## Move the remaining agents onto LiteLLM embeddings

Done and measured for the lab agent (lab_notes.md F9): `gemini-embedding-2` via
LiteLLM, 3072 dims, semantic retrieval verified, and the temporary embeddings pod
deleted. #3575 added the models, #3576 granted them to the openclaw key.

Two consumers left, and both are a config change rather than new infrastructure:

- **`public-coder-agent`** has no embedding backend at all. It cannot reach
  `api.openai.com` and should not gain that route, so LiteLLM is the only sane
  option for it.
- **The main `openclaw` gateway** still uses a direct OpenAI Platform key
  (`OPENAI_API_KEY`, `memorySearch.provider: openai`). Moving it puts embeddings
  through Langfuse with every other model call and retires a standing credential.
  **This one is a re-embed, not a config edit**: its index is populated and the
  two embedding spaces are incompatible, so plan for `openclaw memory index` to
  rebuild and confirm recall afterwards. Retire
  `cluster/k8s/agents/openclaw/gateway-secrets/openai-api-key.sops.yaml` only
  after that is confirmed.

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
see the rough edge in [lab_notes.md](lab_notes.md). The point of the TODO is to
stop relying on remembering.

Scope: both configs that declare OpenClaw settings --
`cluster/k8s/agents/public-coder-agent/app/openclaw.json` and the `spec.config.raw`
block of `cluster/k8s/agents/openclaw/gateway/openclawinstance.yaml`. The second is
easy to forget because it is a CRD field rather than a config file, and it is the
production agent.

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

## Find what wedged production's OpenShell sandbox

`kubectl exec` into a sandbox pod reproduces the wedge exactly and permanently,
but that is not what happened in production on 2026-07-28 — nobody exec'd into it.
Ruled out by test since: the `process` tool alone, ordinary `exec` use, six
abandoned yielding background sessions, and a `gh`/`git` probe.

Until the trigger is known, an OpenShell-backed agent cannot be trusted
unattended: the failure is silent, permanent, and reachable by something ordinary.
Full write-up in
<../../cluster/docs/lessons_learned/2026_07_28_openclaw_sandbox_clone_loss_and_ssh_orphan.md>.

## Detect an agent routing around a broken security control

The agent hit a broken TLS trust chain and silently switched to
`curl -k` / `git -c http.sslVerify=false` for the rest of the run rather than
reporting it (F8). Nothing surfaced that. Containment held — destinations are
enforced at `CONNECT`, before TLS — so this is an integrity and observability gap,
not an escape.

Cheap detector, since every request already transits our mitmproxy: the proxy sees
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
