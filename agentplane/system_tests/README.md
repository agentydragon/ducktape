# Agentplane system tests

The system test scenarios are **vertical tests of a running deployment**: they create real
sandboxes through the app's HTTP API, open sessions on the real harnesses, and assert on
cross-component behavior and durable system evidence. They are not unit tests with a live backend —
they are the checks that the deployed system does what <../egress/SPEC.md> says it does and that a
session's standing instructions reach the model that serves it.

The four CI suites each start one [disposable k3s stack](k3s/README.md) in an RBE action.
The session-scoped `deployment` fixture exposes its cluster handle, app URL, token, and MCP access
([`system_test_target.py`](k3s/system_test_target.py)). Compatible scenarios share a stack:

| Target                                             | Scenarios                                                                                                    |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `//agentplane/system_tests/suites:test_pod`        | egress, messaging, presets, session instructions, suspend/resume, thread stream, Pod credentials and storage |
| `//agentplane/system_tests/suites:test_dispatcher` | Dispatcher queue, wake and replica behavior                                                                  |
| `//agentplane/system_tests/suites:test_mcp`        | MCP integration, dynamic client registration and Pod central sampler                                         |
| `//agentplane/system_tests/suites:test_vm`         | VM Sandbox behavior, runner credentials, systemd supervision and VM central sampler                          |

The shared stack cuts setup and image-import work, while each target now has a coarser cache key: a
changed scenario reruns its suite. Scenario modules live in [`scenarios/`](scenarios/) as ordinary
libraries. Entry points in `suites/test_*.py` contain ordinary pytest tests, marks, and
parametrization. Each test passes its fixture values explicitly to a plain scenario function.
Gazelle derives Python dependencies; `k3s_suite` supplies stack artifacts and execution settings.

The session-scoped `deployment` fixture yields the running stack. Suite client fixtures depend on
it directly, and executor fixtures declare the suite's SandboxTemplate and working directory.
Storage layouts and sampler execution probes remain owned by their respective
scenario modules. VM probes retain the systemd, UID, and cgroup assertions. VM test markers enable
tracing and diagnostics; they do not select deployment configuration.

The build supplies complete service and guest image lists. The pytest adapter resolves these and
the rendered deployment once into `StackInputs`; stack startup does not discover image layouts by
scanning runfiles. Kubernetes helpers take an explicit `Cluster` containing the kubeconfig, namespace,
and kubectl command. Only the live-test fixture reads ambient deployment settings.
Per-test fixtures create and clean up their own Sandboxes and resources. A host-side provisioner
allocates a fresh loop-backed volume for each VM workspace claim UID, retaining it until stack teardown.
Suites do not declare volume counts; deleting and recreating a claim never reuses its workspace data.

Pod and MCP suites remain separate so their larger scenario sets can run on different workers.
The sampler shares MCP's stack rather than paying for another k3s startup.
Within each suite, independent Claude/Codex cases run together through
[`run_harness_pair`](harness_pair.py), sharing the test's clients and scripted model server.
Each harness still creates its own Sandboxes and runs every scenario assertion. Seven Pod pairs
and four MCP pairs overlap; the messaging and launcher pairs each peak at four Sandbox Pods.
Storage remains sequential because it requires an empty Sandbox inventory and changes shared
configuration. MCP OAuth linkage remains parametrized and sequential because both harnesses
update the same operator/server record. A pair failure names its harness and cancels and awaits
its peer before fixture cleanup.
The Dispatcher suite remains separate because replica coverage changes the shared Runner Gateway
Deployment and deletes a recorded Pod while checking controller behavior.

`test_vm` runs on a stack with KubeVirt emulating the guest CPU and the k3s test guest
([VM Sandboxes](k3s/README.md#vm-sandboxes)). It starts six VM Sandboxes concurrently: one
scripted turn per harness through the launcher's egress relay, plus Codex tool OOM containment and
its successful follow-up command with the same Runner and app-server, two credential guests,
and the VM central sampler. All six Sandboxes boot independently on an 8-unit executor with
room for their 1.5 GiB guest allocations and the k3s services. Each harness scenario checks workspace ownership,
streams, identity, security settings, resource budgets and detached-child cleanup through ordinary
Session setup. Each concurrent VM scenario scopes its own client and Sandboxes, captures diagnostics
before deletion, and starts cleanup when its assertions finish, overlapping its peers. The suite
awaits all cleanup before stopping the shared stack; peer cancellation also waits for
teardown. The scripted server remains shared and routes by Pod identity.
The Claude guest uses `runner-vm-supervision`, selecting the remaining private Go
supervisor checks before Runner starts, on the same root image as every other guest. There is no
additional supervision VM or root image. `test_vm_manual` covers
VM workspace ownership, suspend/resume and storage by hand. `test_real_model`
runs the real-model egress and session-instruction cases with the BuildBuddy live-test key
([Real model on k3s](#real-model-on-k3s)). Two other checks need what only a deployment has and run
against staging by hand ([Running live](#running-live)).

The rendered stack selects the model backend. The `test_real_model` stack
and a deployment serve a real model. On the other stacks, LLM ingress forwards to the scenario itself, which plays the model
([`scripted_upstream.py`](scripted_upstream.py)): the `scripted` fixture hands a scenario the
`ScriptedModel` of its own Sandbox's Pod, and the scenario answers each request and asserts what
the harness sent. Suites explicitly select their cases; a case marked `real_model` or
`scripted_model` fails setup if its stack serves the other backend. Only cases whose evidence
is what a model does are `real_model`. Codex is offered a ChatGPT route there
too, so it runs in code mode, which the scripted model answers.
Every request the scripted model receives is kept in the target's `test.outputs/scripted_upstream/`.

```bash
bbr test \
  //agentplane/system_tests/suites:test_pod \
  //agentplane/system_tests/suites:test_dispatcher \
  //agentplane/system_tests/suites:test_mcp \
  //agentplane/system_tests/suites:test_vm
```

Fast offline driver tests cover login choreography (`test_operator_login`), startup retries and
terminal-turn handling (`test_agent`). They remain standalone tests, create no Sandbox and do not
prove that a deployed IdP, Agentplane, or harness is reachable. Operator login is setup for the MCP
scenario.

The lower-level `//agentplane/runner:test_pid1_container` runs the direct PID1 and subprocess Pod backends
as real container PID 1. It checks native exit, TERM, abrupt PID1 death, escaped descendants and
replacement after observed runtime exit. The shared Sandbox template explicitly selects the direct PID1 backend
for the Pod system tests. The outer replacement boundary is described in the Runner's
[supervision contract](../runner/docs/supervision.md#pid-1-and-the-outer-fence).

`Agent.run` returns only for `TURN_STATUS_COMPLETED`. Any other terminal status fails immediately
with the turn id, status and quoted runner diagnostic. Its collector also stops at a new
`command_failed` (matching the awaited command when one is specified) or a `harness_launch_failed`
after the awaited command is admitted, before scenario assertions about tool effects. It does not dump
prompts, tool output or native frames, or automatically resend the admitted input.

The general egress and instruction scenarios run on **both harnesses**. The runner protocol is the
same for Claude and Codex, so the same scenario function covers both. Both `offered_model` and
`run_harness_pair` ask the deployment which models it offers for each harness. The presets scenario instead exercises a Codex launch preset's
defaults, Sandbox binding, bootstrap marker, inherited fields, and local override, with the scripted
model only. The k3s fixture configures that preset (`launch_preset.py`) in place of the deployment's.
Initial session Open uses a 120-second retry budget for HTTP 503, retaining the same session id;
refusals and other server failures are not startup readiness signals.

## Thread stream

The `thread_stream` scenario is a browser's view of a Thread with the scripted model: every tab follows one
archived log by event id, a reconnect resumes after its `Last-Event-ID`, a command id answers only
the command it first named, and turns nobody watches are archived, a failed one included, with the
next input its own turn.

## Thread latency

`test_thread_latency` times a one-word turn on staging at low reasoning effort and holds what agentplane adds
to the harness's own reported turn to half a second, then times the browser's reads to open that
thread, per stage, cold and warm, against regression ceilings; its module docstring defines each
interval. Each run writes its timings to
`bazel-testlogs/agentplane/system_tests/test_thread_latency/test.outputs/`.

## Phase tracing

Every case writes OpenTelemetry spans to `test.outputs/vm-phases.jsonl` (named for the VM suite that
introduced it) using the shared
[test trace exporter](../../util/testing/otel_tracing.py). Each `vm.case` trace carries the pytest
node ID and covers function-scoped fixtures, scenario execution, diagnostics and cleanup. Shared
k3s startup remains in `k3s-timeline.jsonl`; it is session setup, outside these per-case traces.

`vm.sandbox.ready` carries an event at each Sandbox and Pod condition's transition time and each
container start, so a slow launch splits into controller, kubelet, runner-connection and polling
time. The spans separate Sandbox create acknowledgement from waiting for Ready, session opening, and
scenario execution. `test_vm` also separates the scripted turn and each OOM command
(`memory-before`, `tool-oom`, `memory-after`); each grouped Sandbox scenario has its own `vm.scenario`
span so their overlap is visible. Cleanup events distinguish suspend acknowledgement,
observed effective suspension, delete acknowledgement, and observed deletion. Nested spans overlap;
do not add a scenario's duration to its child turn/command durations. Use `test.xml` for the final
pytest verdict; a case span includes fixture lifetime and is not itself a pass verdict.

Completed spans flush immediately, including exception status, so earlier phases survive a later
failure or process kill. An unfinished span is not exported if the process is killed. Attributes
contain test/Sandbox identities and phase names; command bodies and model transcripts are not
added to the trace attributes.

K3s suites also write `test.outputs/process-spans.jsonl` from a fixture-owned OTLP receiver. The
stack exposes it at the production `monitoring/alloy` Service name, so service, Pod Runner and VM
Runner exporters use their usual configuration without an external collector. Each line records
the emitting service, span name and timing, trace/span/parent IDs, outcome, and selected identity
and numeric attributes. The test HTTP client propagates the active `vm.case` trace context through
the app; asynchronous controller work can start a separate trace, so join those spans by
`session.id`, `thread.id` or Sandbox identity. The receiver flushes each export immediately; SDKs
batch their own exports, so a process killed without shutdown can still lose its final spans.
Request bodies, model transcripts, headers and authentication values are not retained.

## VM workspace ownership

The `vm_workspace_ownership` scenario creates one uniquely named VM Sandbox and
opens two Codex Sessions without sending a model turn. The first setup checks that the
second Session's workspace path is absent; the second exercises normal runner setup, checks UID/GID
107 ownership, then runs `git init` and `git rev-parse` with global and system Git config disabled.
Fixture teardown suspends and deletes only the Sandbox created by that invocation.
`test_vm_manual` runs it on a stack of its own and is `manual`:

```bash
bbr test //agentplane/system_tests/suites:test_vm_manual
```

The result is written to the target's `test.outputs/vm-workspace-ownership.json`.

## Workspace storage layout

The `storage` scenario is a model-free fixture for the
built-in Pod `DirectoryTree` and VM `BlockFilesystem`/ext4 workspace paths. The VM workspace uses
the k3s stack's loop-backed raw block class while VM state remains on local-path filesystem
storage. It checks Sandbox-owned PVC identity, `Block` volume mode and layout records, marker bytes
across suspend/resume, refusal of an incompatible claim before workload start, and retained data
after VM replacement. It creates only its own
Sandbox, template and workload, then removes them through UID-preconditioned lifecycle operations.
It refuses a stack that already has Sandboxes. `test_pod` runs its Pod case as part of the shared CI
stack; `test_vm_manual` runs the VM case on its own stack:

```bash
bbr test //agentplane/system_tests/suites:test_vm_manual
```

The suite writes `test.outputs/storage-{pod-directory,vm-raw}.json`. The VM suspend/resume case in
`test_vm_manual` uses
the same block-backed VM fixture while verifying the runner's persisted native sessions across the
launcher replacement.

## Suspend and resume

The `suspend_resume` scenario tests two Threads (Claude and Codex) in one Sandbox with the scripted
model. `test_pod` runs the Pod case; `test_vm_manual` runs the VM case. Each suspends until the
old Pod or launcher is gone, then resumes the original Threads by sending their next input.
Each case verifies all Sandbox-owned state and workspace PVC UIDs and bound PVs survive, the
incarnation changes, the sealed archived history remains byte-for-byte unchanged, and one
`JournalReopened` continues its source sequence before the resumed native turn. It also checks that
Open reserves a Session without a harness process, that a completed turn has a ready process
assignment, and that the new runner retains dormant Sessions and their native IDs before the next
input. While the Sandbox is suspended, an exact retry of each seed input is answered from the archive. The
resumed Threads must send the model their own seeded token and not the other's. Intentional delta
settlement may leave gaps in archive cursors; retained entries remain ordered and unique.

Successful cases write PVC/PV identities, old/new incarnation UIDs, Session IDs and seal/reopen/
continuation cursors to the target's `test.outputs/{runner,runner-vm}-resume.json`. Checkpoints are
saved during the run, with `passed: false` until all assertions succeed, so partial evidence survives
a later failure. The memory code is explicitly synthetic and non-secret. The scenario creates uniquely named
Sandboxes, and fixture teardown suspends and deletes only the Sandboxes that invocation created.
It does not delete an existing Sandbox. This completed-turn scenario does not cover a crash at a
command checkpoint or interrupted setup; deterministic coverage for those boundaries lives in
`//agentplane/runner:test_restart` and `//agentplane/runner:test_seal`.

## Agent messaging

The `messaging` scenario has one agent message another, each in its own Sandbox, on each harness. The
sender holds the fixture's [`send_grant`](send_grant.py) PolicySet, a `session_inbox_access` rule to an
inbox tag the recipient carries, and runs `agentplane notifications send`; the recipient reads and
acknowledges its inbox when the platform's notice starts its turn. It asserts the entry's authenticated
sender and the sender's view of its message: covered while the recipient holds the notice, acknowledged
after. The k3s fixture uses a one-second notification quiet window and a two-second maximum wait;
the Notification Service's worker integration tests cover debounce deadlines.

## Dispatcher queue and replicas

These probes target the Runner Gateway. The Dispatcher
Service remains `session-dispatcher:8080`; replica operations target the `runner-gateway`
Deployment, and direct Pod probes use the shared gRPC listener on `8080`.

The `queue` case checks authenticated submission to a stopped Sandbox, durable queueing, and
runner admission after an explicit start. It uses a fixture Sandbox and the stack app's workload
identity for TokenReview; it does not call app HTTP or run a model turn. One case alone:

```bash
bbr test //agentplane/system_tests/suites:test_dispatcher --test_arg=-k --test_arg=queue
```

The `replicas` case covers replica takeover and deletion broadcast. It queues a setting change in a
fresh stopped Sandbox, temporarily scales the Runner Gateway
from one replica to two, deletes the original owner's Pod with a UID precondition, and observes
Lease takeover through a watch. The retained command is then delivered after explicitly starting
the Sandbox. Finally, streams pinned to both replica Pods must receive the queued command's
terminal deletion outcome. No model turn or app HTTP API is used. Authentication uses a short-lived
token bound to an existing app Pod, so an app rollout during the probe revokes that token.

The fixture Sandbox is deleted by UID; the failover case deletes only the recorded owner Pod. The
original replica count is restored if the Deployment UID and generation still match the temporary
scale, preserving concurrent GitOps/operator changes. Evidence is written to
`test.outputs/dispatcher-deployed.json`; `passed` and `cleanup_complete` are distinct. The `queue`
case retains the original single-replica delivery check.

The `opted_in` case covers send-triggered wake for both app `OpenSession` and notification
`SubmitInput`. It creates one stopped Sandbox with `wakeOnSend: true`, then concurrently submits an app
`OpenSession` and a notification `SubmitInput` using short-lived tokens bound to existing Pods. Both
senders must share one `wakeGeneration`; the Dispatcher waits for a new Ready incarnation before
admitting the Session and input. The `queue` case covers the default `wakeOnSend: false`, which
leaves a send queued without changing Sandbox state. New Sandboxes default to false; the app's
create and edit controls let the operator opt in explicitly, and changing the setting alone does
not wake a Sandbox. The Dispatcher charges at most ten
distinct wake generations per authenticated caller per rolling minute.

## Central sampler

`test_mcp` and `test_vm` each run the central sampler scenario on their existing stack,
using a fresh Pod and VM Sandbox respectively. Each submits a central Session and user input
through the deployed Dispatcher, runs setup and a model-requested shell command through the
Sandbox's holding Gateway and pinned exec-server, then verifies the reasoning, final answer, setup
output, and command evidence retained by authenticated History ingress. The VM tool also checks the
real systemd guest's PID 1 and that the executor runs in its systemd-managed executor cgroup. The
History reader must report the same Session and Sandbox UID and fold the completed turn into
reasoning, assistant, tool, and confirmed-input entities. The tool reads the file setup created, so
the assertion requires both operations to reach the same executor workspace.

The scenario deploys the sampler through the shared optional cdk8s construct, with its own
PostgreSQL database and Kubernetes TokenReview. An initialization container applies its schema; the
serving container uses in-cluster Kubernetes credentials and projected Gateway, History, and LLM
workload tokens. The app catalog, Dispatcher admission, Gateway executor path, History ingress, and
LLM ingress caller policy are rendered with the sampler. The sampler sends its scripted Responses
requests through LLM ingress to the test node, so the scenario exercises that authenticated provider
boundary as well. Two logical Sessions share one lazily started Codex host; each code-mode cell uses
a fresh V8 isolate, while the test overlaps turns and interrupts one Session independently. Pod
state, init/service logs, and shared-host logs are retained in test outputs. The
execution/History summary is written to `test.outputs/central-sampler-k3s.json`.

```bash
bbr test //agentplane/system_tests/suites:test_mcp --test_arg=-k --test_arg=central_setup
bbr test //agentplane/system_tests/suites:test_vm --test_arg=-k --test_arg=central_setup
```

## MCP integration

`test_mcp` runs the MCP integration and dynamic client registration scenarios on a stack with Dex,
the standalone MCP edge, `mcp-everything` and the Dex-backed OAuth MCP fixture
(`test_mcp` uses the MCP fixture stack), configured under test-only names
([`mcp_servers.py`](mcp_servers.py)). The DCR scenario checks deployed discovery and public-client
registration using plain HTTP; `//agentplane/mcp_edge/authserver:sdk_dcr_test` owns the real Python
SDK contract against production handlers and PostgreSQL ([`dcr.md`](dcr.md)). The MCP scenario
exercises Actions calling its upstream MCP fixtures. In that scenario the scripted model runs one
`agentplane --json actions …` command per turn, on each harness, and the scenario reads the printed
receipt back from what the harness sent the model.

- `test_agent_executes_mcp_action` binds its Sandbox to an `exact_actions` set naming the
  `mcp-everything` echo, submits it with `--wait` and checks the exact upstream echo result.
- `test_policy_binding_auto_approves_the_bound_sandbox` writes a `PolicySet` approving that echo
  with a `message` of at most 200 characters and a `PolicyBinding` naming the Sandbox's
  ServiceAccount, then waits for the binding's `ActionsReady`. A match comes back auto-approved, and
  the BFF receipt's Decision names the binding, the set and the rule; an over-long message waits for
  the operator; once the expired binding is deleted, a match waits too. Pending requests are denied
  through the BFF.
- `test_agent_mcp_bff_decision` submits from an unbound Sandbox, reads through the app's generated
  `/upstream/actions/v1/action-requests/{id}` facade and decides through its operator decision RPC,
  then has the agent `wait` for the outcome. Each harness denies a request in a fresh session, then
  allows a new request in that same session. Each verdict has its own marker and idempotency keys,
  and checks the request/Decision/Execution snapshots, the operator's identity, duplicate-decision
  idempotency and stale-version rejection.
- `test_operator_links_oauth_mcp_server` links the OAuth fixture through the BFF, following Dex's
  local-password form in a separate fresh browser that stops at the linkage callback; the BFF then
  validates the unchanged state and completes its own token exchange. The agent then calls the
  protected tool.
- `test_launcher_drives_only_the_sandbox_it_launched` has an agent launch a Sandbox through the
  `orchestration_v1` group with grants on it, which the operator approves through the BFF. Its
  `open_session`, `submit_input`, `read_turns`, `wait_for_turn` and `get_session` on that incarnation
  then come back approved by the `<sandbox>.launcher` PolicySet. The test reads a token before the turn,
  checks a short wait returns without completion, then uses its updated token to wait for completion
  and reads the recorded input, tool calls and final assistant message. The launched Sandbox's
  own request for the same read, one naming another UID, and one after the Sandbox is deleted all
  wait for the operator, who denies them. The launcher grant auto-approves a read of a Session from
  another Sandbox, and the executor refuses that mismatch.

These cases receive the operator Secret name from `deployment.mcp_fixtures` and read only that Secret
via `kubectl get --raw`, using the explicit cluster handle naming the k3s fixture's apiserver. No pre-issued cookie is
used. A missing Secret or malformed Secret data fails **BLOCKED**, without printing kubectl output or
decoded values.

The Secret's `login` is the identifier submitted to Dex; `username`
is the expected operator name returned by the app. Dex's local password connector
uses the configured email for login, which is distinct from its display username.
The k3s fixture chart writes these fields beside Dex's config.

A fresh `httpx` cookie jar starts at the app's `/auth/login`, follows Dex's redirects through
its ordinary local password form and returns to the app's `/auth/callback`. The app owns OAuth
state, nonce, PKCE, token exchange and server-side session storage. Mutation requests use
the app's exact same-origin `Origin`. The test never fabricates a cookie or inserts a session.
The app (over http) and Dex (over TLS from a CA the stack makes and the test trusts) serve on
their in-cluster Service names, which the stack resolves for the test process; `operator_login`
allows http only to such names.

Credentials stay in process memory. HTTP logging is suppressed for the BFF client's
lifetime; pytest local-variable dumps are refused before Secret access. Do not add
HTTP tracing, response dumps or credential artifacts. The existing suite URL/token
settings below still serve the separate workload client. The agent-pod runner
restriction below still applies: remote choreography tests are not live runs.

After login, a fresh absent-Action lookup checks federation without listing existing
requests. Its `404` must carry the BFF's structured upstream error for that exact
operator Action request path, not an arbitrary ingress, signing-key, or login error.
Malformed or unexpected responses fail without exposing their contents.

## Real model on k3s

`test_real_model` starts the k3s stack with LLM ingress forwarding to the public LiteLLM. The
app offers Haiku to Claude Code and luna to Codex, both on the lane of the BuildBuddy live-test key,
which BuildBuddy decrypts into that test action alone as `OPENAI_API_KEY`
(`test.env-secrets`, as for `live_openai_py_test`). It is tagged `live_openai_api`: PR CI builds it and
skips it, and `--config=live-openai` runs it:

```bash
bbr test --config=live-openai //agentplane/system_tests/suites:test_real_model
```

That target runs the egress probe, whose evidence is what a real model works out. The probe's
Sandbox discovers and presents the fixture's own credential ([`probe_credential.py`](probe_credential.py)),
a token for a ServiceAccount holding no RBAC, which the Kubernetes API names back only if the proxy
substituted it. The egress scenario in `test_pod` runs the other egress cases with the scripted model: the model call goes
through the proxy, and a PolicySet bound to a running Sandbox takes effect. The latter binds the
fixture's own PolicySet ([`runtime_grant.py`](runtime_grant.py)), not one a deployment's Sandboxes use.

## Running live

`test_thread_latency` (Electric's sync path) and `test_ollama_routes` (the GPU Ollama) need what only a
deployment has, and run against staging. They are not in CI and not on RBE: they are `manual`, so
`//...` never selects them, and they need a kubeconfig and a route to the cluster. Name one explicitly:

```bash
bazelisk test //agentplane/system_tests:test_thread_latency --test_output=streamed --test_arg=-s
```

`//agentplane/system_tests:test_ollama_routes` exercises every configured Ollama chat
route offered by the committed staging app ConfigMap, on each harness that offers it (Codex alone today),
with the 128k cases first. Collection reads the generated manifest, not cdk8s Python;
each case still checks that the live app offers the route before opening a session. Each cell creates a Sandbox,
opens a real session, and requires recorded shell-tool output. It has an absolute
300-second turn limit; a backend that never completes still leaves an interrupted
turn rather than a model verdict. To run just one cell, set `OLLAMA_SMOKE_CASE` to
its pytest id, for example
`harness_codex-ollama-oai-chat-gpt-oss-20b-128k`. Each cell writes a small JSON
result under the target's `test.outputs/` directory. A Bazel rerun of this target
replaces local test outputs, so save any evidence needed across runs first.
Results include context windows reported by the native harness (Codex token-usage
notifications and Claude result model usage). These can differ from server context:
for example, Codex may report its usable budget after reserving headroom. An empty
list means the harness did not report a window in the observed turn.
For transient sidecar diagnosis, `--test_env=OLLAMA_SMOKE_HOLD_SECONDS=30` retains
the isolated Sandbox for at most 30 seconds after the turn result is written, before
normal fixture cleanup.

By default they test `https://agentplane-staging.allegedly.works` and mint their own bearer token with
`kubectl -n agentplane-staging create token agentplane-agent --audience=agentplane`. That call needs
RBAC on `serviceaccounts/token`, and the app only admits subjects its `AGENTPLANE_TOKEN_SUBJECTS`
names, so a token for any other ServiceAccount is refused with `403`.

Override any of it through the environment:

| Variable                                 | Default                                      |
| ---------------------------------------- | -------------------------------------------- |
| `AGENTPLANE_SYSTEM_TEST_URL`             | `https://agentplane-staging.allegedly.works` |
| `AGENTPLANE_SYSTEM_TEST_TOKEN`           | minted with `kubectl`                        |
| `AGENTPLANE_SYSTEM_TEST_NAMESPACE`       | `agentplane-staging`                         |
| `AGENTPLANE_SYSTEM_TEST_SERVICE_ACCOUNT` | `agentplane-agent`                           |

Do not copy credentials into the checkout or pass them as command-line arguments.

### Controlled-host preflight

Run the suite from a controlled NixOS host, devbox/VM, or FHS-compatible agent
container. Do not use `bbr`/`bb remote` for this suite: the completed test
process must stay on the caller, where it can use the caller's Kubernetes
credentials and network path. BuildBuddy-hosted runners do not inherit the
caller Sandbox workload token or the current egress substitution path.

Before starting a long run, check the client-side seams separately:

```bash
command -v bazelisk kubectl
bazelisk version
kubectl config current-context
kubectl -n agentplane-staging auth can-i create serviceaccounts/token/agentplane-agent
kubectl -n agentplane-staging create token agentplane-agent \
  --audience=agentplane --duration=600s >/dev/null
```

The preflight must not print or save the returned token. If the token command
fails, fix caller RBAC or kubeconfig before launching the suite. If Bazel
fails while loading the module graph, fix the caller's Bazel/repository-rule
runtime before investigating staging.

### Failure classification

Use the first point at which the run fails to choose the next investigation:

| Observation                                         | Likely seam                                                                  |
| --------------------------------------------------- | ---------------------------------------------------------------------------- |
| No `systest-*` Sandbox is created                   | Bazel client, module/repository rules, kubeconfig, or token setup            |
| Sandbox is created but never becomes ready          | Scheduling, image pull, runner bootstrap, or staging capacity                |
| App rejects the initial API request                 | Token audience, subject allowlist, or app ingress                            |
| Model turn hangs and the decision history is empty  | Sandbox proxy environment, proxy route, or model ingress path                |
| Ring records a deny for an expected destination     | Egress policy/binding or destination URL mismatch                            |
| Test assertions pass but teardown reports a failure | Runtime cleanup/reconciliation; inspect the named Sandbox before rerunning   |
| Process is killed and `systest-*` Sandboxes remain  | Expected teardown limitation; clean them up deliberately before the next run |

Keep the complete test output and the proxy/app decision evidence together.
The model transcript explains what the agent attempted, but the decision history
is the authority for what the proxy actually served.

### Where an agent can run it

Agent pods must use `bbr`/CI, never local Bazel or pytest. The live checks are
`manual` / `no-remote-exec` and lack an approved CI runner with staging identity and
connectivity. Therefore it is **blocked from agent pods**; do not disable remote
execution/caching or mint substitute credentials to work around that boundary.
The controlled-host instructions above are operator-only, not an agent-pod fallback.
See [repository instructions](../../AGENTS.md).

Teardown subscribes to `/live/sandboxes` until a fresh watch snapshot reports effective `Suspended`
mode for the created Sandbox's name and UID, within the existing readiness bound. The spec write
alone does not satisfy deletion admission. Authorization refusals fail immediately; teardown
does not poll the inventory, reconnect a dropped stream, or delete a replacement Sandbox.
Afterwards, check that nothing leaked: `kubectl -n agentplane-staging get sandboxes.agentplane.allegedly.works`
should show no `systest-*`.

## What it costs

`test_real_model` runs a few turns on the BuildBuddy live-test key (Haiku and luna) on a stack of
its own, so a run is minutes and a few cents. A live check's Sandboxes are suspended and deleted in
fixture teardown, including after a failure; a teardown that cannot delete one fails loudly, because a
leaked sandbox holds a PVC and a node slot in staging.

## TODO: sweep sandboxes a killed run leaks

Fixture teardown suspends and deletes every sandbox a scenario created, including after a failure.
It cannot run if the process is killed outright — a Bazel timeout, a `^C`, a dropped connection —
and each leaked sandbox holds a PVC and a node slot in staging until someone notices.

What that wants is a sweep at session start: list the sandboxes whose names carry this suite's
`systest-` stem, and delete any older than a run could plausibly be. Deliberately not built yet,
because the stem is the only marker and a real sandbox someone named `systest-something` would be
destroyed by it — a label the app sets on suite-created sandboxes, or a dedicated namespace, is the
thing to add first.

## Why it asserts on the ring, not on the agent

The agent's own account of a tool call is prose. "I fetched the repository" is equally consistent
with a request the proxy admitted, a request that never reached the proxy, and a model that did not
run the command at all. The proxy's decision history is the system's record of what it admitted,
so that is what a scenario checks; the turn's output is carried into the failure message, where it
explains a failure rather than deciding one.

Where a scenario has to look _inside_ the sandbox, it never asks the model to print a secret. A
model that declines to echo a credential, or redacts it, produces output with no credential in it —
which would satisfy an "is it absent" assertion for entirely the wrong reason. The command prints a
verdict token instead, and the scenario fails unless one of the two tokens actually comes back, so a
refusal reads as a failure rather than as an absence.

This suite exists because the last gap of that shape — a runner that dropped the sandbox's proxy
variables, so every call bypassed the proxy and hung with an empty history — sat behind a fully green
unit suite until someone drove the deployed app by hand.

The real-model case of the session-instructions scenario is the one scenario that cannot follow the rule:
whether a model obeys the instruction is what it tests, so the model's reply is its evidence. Its
answer to that is a marker token no model emits on its own plus a control session, opened with no
instructions on the same sandbox and given the same prompt, that must not produce it. Its scripted
case asserts what each session sends the model instead.
