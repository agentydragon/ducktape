# Agentplane system tests

These tests exercise a running Agentplane deployment: they create Sandboxes through the app,
run Sessions on the harnesses, and check cross-component behavior and retained evidence. They
cover deployment behavior beyond what unit tests can establish; the egress contract is described
in [the egress spec](../egress/SPEC.md).

## Where things live

| Path                                    | Owns                                                 |
| --------------------------------------- | ---------------------------------------------------- |
| [`suites/`](suites/)                    | CI, manual and live test entrypoints                 |
| [`scenarios/`](scenarios/)              | Behaviors and assertions against a deployment        |
| [`driver/`](driver/)                    | Session, Sandbox, API, model and concurrency helpers |
| [`deployment/`](deployment/README.md)   | Fixture charts, workloads and stack lifecycle        |
| [`k3s/`](k3s/README.md)                 | Retained test node, bootstrap and networking         |
| [`firecracker/`](firecracker/README.md) | Firecracker lifecycle adapter and KVM acceptance     |
| [`vm/`](vm/README.md)                   | VM runtime, storage, guest image and evidence        |
| [`debug/`](debug/README.md)             | Dated investigations and evidence, with `k3s/debug/` |

Offline tests stay beside the helper they exercise. The deployment guide owns stack setup and
artifacts; the VM guide owns guest details. The suite BUILD files and test modules are the source
of truth for exact case membership.

## Suite entrypoints

Each k3s system-test target starts one fixture stack. Scenarios share it only when their resource
changes are compatible. `test_dispatcher` owns a separate stack because it scales the `sessions`
Deployment and deletes one of its Pods. The external MCP delegation acceptance owns another stack
because it scales the app to zero after OAuth consent.

| Target                                                                 | Coverage                                              |
| ---------------------------------------------------------------------- | ----------------------------------------------------- |
| `//agentplane/system_tests/suites:test_pod`                            | App-backed Pod Sandbox scenarios                      |
| `//agentplane/system_tests/suites:test_pod_platform`                   | Native Pod workload trust and Egress credentials      |
| `//agentplane/system_tests/suites:test_session_deletion`               | Session deletion and replacement                      |
| `//agentplane/system_tests/suites:test_dispatcher`                     | Dispatcher queue, wake and replicas                   |
| `//agentplane/system_tests/suites/mcp:test_actions`                    | MCP Actions and operator decisions                    |
| `//agentplane/system_tests/suites/mcp:test_services`                   | App-backed delegated Action admission                 |
| `//agentplane/system_tests/suites/mcp:test_services_mcp`               | MCP discovery, consent and policy binding             |
| `//agentplane/system_tests/suites/mcp:test_services_central`           | Native central-sampler service paths                  |
| `//agentplane/system_tests/suites/mcp:test_sampler_roll`               | Sampler replacement with live executor work           |
| `//agentplane/system_tests/suites/mcp:test_gateway_crash`              | Abrupt Runtime proxy loss during a live executor read |
| `//agentplane/system_tests/suites/mcp:test_history_outage`             | History outage during an active central-sampler turn  |
| `//agentplane/system_tests/suites/mcp:test_delegation`                 | External MCP delegation                               |
| `//agentplane/system_tests/suites/mcp:test_parent_delegation`          | Parent-to-child Session CLI flow                      |
| `//agentplane/system_tests/suites:test_vm`                             | Required Claude and Codex VM scenarios on hosted KVM  |
| `//agentplane/system_tests/suites:test_vm_tcg`                         | Same VM cases on managed QEMU, manual comparison      |
| `//agentplane/system_tests/suites:test_real_model`                     | Real-model egress and Session instructions            |
| `//agentplane/system_tests/suites:test_vm_manual`                      | Manual VM storage, ownership and suspend/resume cases |
| `//agentplane/system_tests/suites:test_vm_oom`                         | Manual Codex tool-OOM survival probe                  |
| `//agentplane/system_tests/suites:test_vm_profile`                     | Manual tool-OOM probe with guest profiling            |
| [`//agentplane/system_tests/x/vm_kvm:test_vm_kvm`](x/vm_kvm/README.md) | Cold KVM comparison, manual                           |

See [CI executor routing](../../devinfra/ci/README.md#where-bazel-ci-runs-tests) for pool selection and the paired evidence.

`test_vm_tcg`, `test_vm_manual`, `test_vm_oom`, `test_vm_kvm` and `test_vm_profile` carry the `manual` tag. The staging checks
[`test_thread_latency`](suites/live/test_thread_latency.py) and
[`test_ollama_routes`](suites/live/test_ollama_routes.py) carry both `manual` and `no-remote-exec`;
the [live-test instructions](#running-live) explain how to run them.

Pod, MCP Actions, and VM harness cases use the same bounded batch runner. Pytest selection determines
which callbacks run; each pytest item reports its own result and traced execution time. Fixtures
own shared deployment/model-server lifetime, while each callback owns its clients, guests, and
cleanup. The storage case carries an explicit exclusivity marker and runs before other Pod cases,
so no runner pair is retained while it allocates guests. A case's timeout bounds its own coroutine,
even while pytest is awaiting a different case.

`case_timeout(seconds)` starts with the scenario, after shared deployment setup. It is separate
from pytest's process-budget watchdog, which still covers setup, execution and teardown within
the Bazel deadline. Batched targets use `junit_duration_report=call`: testcase durations contain
the actual concurrent callback time (including its owned cleanup), while JUnit's suite duration
retains total elapsed time. Pytest's `--durations=0` reports the separate setup/call/teardown
durations; shared setup and teardown appear against the items that request/finalize the fixture.
Concurrent testcase durations can sum to more than the suite's wall time.

The common [`conftest.py`](conftest.py) owns shared tracing, harness selection and scripted-model
fixtures. App-backed tests explicitly load [`app_fixtures.py`](app_fixtures.py) for App clients,
model discovery and warmup, per-scenario resources and App Sandbox ownership; MCP OAuth tests load
[`operator_fixtures.py`](suites/mcp/operator_fixtures.py). The neutral [`sandbox.py`](driver/sandbox.py)
contains shared template constants and the descriptor callable type. App lifecycle behavior lives in
[`app_sandbox.py`](driver/app_sandbox.py), with its focused tests in
[`test_app_sandbox.py`](driver/test_app_sandbox.py) (`//agentplane/system_tests/driver:test_app_sandbox`).

The Pod batch has four sequential workers, each owning one lazily created Sandbox.
For matched concurrency experiments, override the slot count with
`--test_env=AGENTPLANE_POD_CASE_LIMIT=2` (or another positive count). Claude and Codex
open separate Sessions on that same Sandbox. Protocol cases reuse their worker's Sandbox; a
fresh-guest case deletes its worker's cache before creating its own guests. Current nonexclusive
cases, including auxiliary impostor guests, need at most two Sandboxes per worker. Storage runs
alone before the workers start. Failed guest cleanup aborts the batch before further allocations.

For current test names and arguments, inspect the suite modules, such as
[`test_pod.py`](suites/test_pod.py), [`test_dispatcher.py`](suites/test_dispatcher.py), and
[`test_actions.py`](suites/mcp/test_actions.py). Scenario modules are libraries called by suite
entrypoints; helper unit tests live beside their implementation. Use the [deployment
README](deployment/README.md) for stack behavior and the [VM Sandboxes guide](deployment/README.md#vm-sandboxes)
for VM image layout and lifecycle.

## Behavior-specific anchors

- [`thread_history.py`](scenarios/thread_history.py) checks concurrent typed History readers,
  cursor resume, command identity, and turns that complete without a reader. History can settle
  streamed deltas, so readers apply reported corrections and compare the retained archive.
- [`suspend_resume.py`](scenarios/suspend_resume.py) checks that a new Sandbox incarnation keeps
  the same storage and archived event prefix while native Sessions continue. Restart and attach
  failure boundaries belong to `//agentplane/env/e2e:test_restart` and `//agentplane/env/e2e:test_attach`.
- The Pod suite also checks explicit resume after idle suspension: the Sandbox UID stays fixed, its
  Pod is replaced, and a prior failed turn remains retained before a new command is sent.
- `test_delegation` continues an external MCP Connection with the app scaled down; the separate
  `test_parent_delegation` case drives a child Session through the actual `agentplane` CLI.
- [`dcr.md`](dcr.md) covers external-client dynamic registration: the SDK component test
  `//agentplane/mcp_edge/internal/authserver:sdk_dcr_test` and the deployed-edge scenario in `test_services_mcp`.
- MCP operator login reads one named test Secret from the fixture API and keeps credentials in
  memory. The caller suppresses HTTP logging and refuses pytest `--showlocals` for the full exchange;
  do not add credential-bearing traces or artifacts ([`test_actions.py`](suites/mcp/test_actions.py),
  [`operator_login.py`](driver/operator_login.py)).
- `Agent.run` returns only after a completed turn. Non-completed statuses and a failed command or
  harness launch stop the case before assertions about tool effects; it does not replay admitted input.
- Pod protocol cases create fresh Sessions, HTTP streams and model-request queues while reusing
  their worker's Sandbox. Queue checks finish before another case uses it; a failed or cancelled
  case discards it. Worker teardown deletes any cached Sandbox before the shared deployment stops
  ([`test_pod.py`](suites/test_pod.py), [`pod_worker.py`](suites/pod_worker.py),
  [`pod_cases.py`](suites/pod_cases.py)).
- Test markers must agree with the selected stack backend; deployment setup rejects scripted-model
  cases on the real-model stack and vice versa ([`pytest_plugin.py`](deployment/pytest_plugin.py)).

### Runtime proxy outages

The `test_gateway_crash` and `test_gateway_budget` targets exercise the Environments Runtime
proxy on `environments:8082`. The shared outage helper removes the Environments Pod, proves its
Service has no ready endpoint, and restores
it after checking retained History progress and the selected failure/recovery boundary.

The abrupt-crash case sends `SIGSTOP` to the exact `sandbox-controller` process, confirms it is
stopped, scales that disposable test Deployment to zero, waits for the Pod's deletion timestamp
and pending `SIGTERM`, then sends `SIGKILL` to the same container ID. The pending signal proves
kubelet began termination. An open process status file guards against PID reuse; cleanup kills
the stopped process even if an intermediate step fails. The `runtime-proxy-crash-{before,process,terminating}.json`
outputs retain this evidence. There is no connection Lease or epoch.

Budget exhaustion still requires an observed failed resume before the model-facing read settles,
explicit uncertain-outcome reporting without mutation replay, continuing History and a subsequent
successful input. Graceful-roll and native-server-loss cases use the same receiver outage helper.

## Model and egress evidence

Model-driven scenarios use either a scripted model served by the scenario or the real-provider
`test_real_model` stack. A scripted result demonstrates the harness-to-scenario path, not external
provider acceptance. The real standing-instructions case uses a marker absent from its prompt and a
control Session with the same prompt but no instructions; the instructed reply must contain the
marker and the control reply must not ([`session_instructions.py`](scenarios/session_instructions.py)). Real-model tests receive
`OPENAI_API_KEY` as a BuildBuddy test secret and carry the `live_openai_api` tag; do not print or
save credentials ([`test_real_model`](suites/BUILD.bazel)).

For proxy behavior, compare the agent's report with the Egress decision history. The report says
what the agent claims to have done; a proxy decision records what the proxy admitted. The egress
scenario checks both, including a private ConfigMap read through the Sandbox's own ServiceAccount
and a denied request to an unlisted host ([`egress.py`](scenarios/egress.py)). For absence checks,
require an executed command's explicit verdict; a model refusing to print a secret is not proof of
absence. The egress case checks the random ConfigMap value as well as proxy decision history.

## Central sampler

The k3s case checks the deployed Dispatcher, Gateway, LLM ingress and authenticated History path
together. Its setup and requested shell command must reach the same executor workspace
([`central_sampler.py`](scenarios/central_sampler.py)); the [service guide](../sampler/README.md)
covers the image and runtime contract. The sampler's HarnessHost and SamplerAdmin RPCs admit exact service SVIDs over mTLS; the
fixture exercises that receiver path while retaining the configured Gateway and History credentials
for outbound calls.

`test_sampler_roll` owns a separate stack. It abruptly replaces the sampler Pod during a confirmed
turn with a live JavaScript cell and running executor process, then checks abandonment ordering,
retained history, one turn settlement, and a subsequent input projected by authenticated History.
The Gateway Pod and executor incarnation stay fixed. This checks successful lost-host reconciliation;
it does not inject the successor startup failure before the driver loads its durable active command.

`test_history_outage` owns another stack. It confirms a central-sampler turn is active, scales only
History down, verifies its ready endpoints disappear and an authenticated History read fails, then
finishes the same model response while the other service Pods and Sandbox runner remain ready. It
compares the sampler journal cursor across the outage with History's committed and folded cursors,
checks ACTIVE projection and exactly-once command settlement, then completes another input.

The Pod suite also creates a native Session using a workload's own SVID: creation is refused
without an explicit environment `bind_session` grant, admitted with the exact Sandbox and
`boundIdentity:self` selector, and retains the Sandbox's live Identity. Retrying the exact creation
UUID after removing the grant recovers its receipt without creating another Session. The
`identity-bind-session.json` output records those checks. Dispatcher integration tests separately
cover delegated actor/creator attribution, grant revocation, concurrent rebind and receipt-only
creator scope across Identities.

## Dispatcher queue and replicas

The queue case leaves a stopped Sandbox unchanged. The replica case scales Sessions to two and
waits for the second Pod to be ready before deleting the initial Pod by UID. It reads the durable
queued command and its eventual admission through the second Pod. Both ready replicas must see
the outcome of a later Sandbox deletion. Opted-in app and Notification submissions share a wake generation ([`dispatcher.py`](scenarios/dispatcher.py)).
Shared Session polling lives in [`dispatcher_operations.py`](driver/dispatcher_operations.py), and
runner mount inspection, archives and cleanup evidence capture live in
[`sandbox_diagnostics.py`](driver/sandbox_diagnostics.py).
The mid-delivery crash case gates the real database settlement after Runtime admission, stops
and kills the exact delivering process after kubelet observes Pod deletion, and observes
recovery through the ready peer after the Session's PostgreSQL lease expires. The initial
singleton identifies the delivering process. The test checks the retained attempt fact,
one Runtime journal admission for the replayed command ID, and a subsequent admitted command.
Both cases restore Sessions to one replica.

The [Dispatcher guide](../session_dispatcher/dispatcher/README.md) owns the command delivery contract.

## Phase tracing

K3s suites use the shared [`tracing.py`](driver/tracing.py) tracer. New stages use it instead of
stopwatch logs. Completed driver spans flush immediately to `test.outputs/test-spans.jsonl`; a killed
test retains completed spans, but unfinished spans are absent. The test log's totals are inclusive,
so concurrent spans overlap. Use `test.xml` for the verdict.

Stack services and Runners export to `test.outputs/process-spans.jsonl`. The fixture-owned receiver
flushes each export immediately, but SDK batching can lose final spans if a process dies before it
flushes. Driver HTTP calls propagate trace context; asynchronous controller or Runner work may start
separate traces, which can be joined by `session.id`, `thread.id` or `sandbox.uid`.

Use bounded identity and outcome attributes only: no credentials, headers, request bodies or model
content. Common case/scenario/readiness and cleanup spans separate Sandbox suspend acceptance,
effective suspension, delete acceptance and actual deletion
([`app_sandbox.py`](driver/app_sandbox.py)). See the
[deployment guide](deployment/README.md) for export paths and the [VM guide](vm/README.md) for retained
guest artifacts. VM journals live under `vm-artifacts/journals/<state-volume>/<machine-id>/`; inspect
or verify them with `journalctl --directory=<machine-id-directory>` or
`journalctl --verify --directory=<machine-id-directory>`.

## Running live

The two `suites/live` targets exercise staging and are marked `manual` and `no-remote-exec`. Run
them directly with `bazelisk` from a controlled NixOS host, devbox/VM or FHS-compatible container
that has the required kubeconfig and network access. Do not send their test process to a remote
runner: it needs the caller's Kubernetes identity and network path. Agent pods must use `bbr`/CI;
they do not have an approved runner with staging identity and connectivity. Keep remote execution
and caching enabled for other tests.

The default deployment is `https://agentplane-staging.allegedly.works` in namespace
`agentplane-staging`. Set `AGENTPLANE_SYSTEM_TEST_TOKEN` to an operator ID token accepted by the
target app; the live suite does not mint an app ServiceAccount token. `test_ollama_routes` reads the
deployed catalog and uses the caller kubeconfig for Sandbox operations. Do not print or save credentials.

| Variable                           | Default                                      |
| ---------------------------------- | -------------------------------------------- |
| `AGENTPLANE_SYSTEM_TEST_URL`       | `https://agentplane-staging.allegedly.works` |
| `AGENTPLANE_SYSTEM_TEST_TOKEN`     | Required operator ID token                   |
| `AGENTPLANE_SYSTEM_TEST_NAMESPACE` | `agentplane-staging`                         |

Check the local seams before a long run:

```bash
command -v bazelisk kubectl
bazelisk version
kubectl config current-context
test -n "${AGENTPLANE_SYSTEM_TEST_TOKEN:-}"
```

The caller-supplied token must not be printed or saved. A Bazel module-loading failure is a caller's
Bazel/repository-rule issue, not a staging failure.

Run a staging thread-latency check with:

```bash
bazelisk test //agentplane/system_tests/suites/live:test_thread_latency --test_output=streamed --test_arg=-s
```

`test_thread_latency` times a model turn and the cold and warm reads used to open its thread; its
module docstring defines the stages and limits. `test_ollama_routes` selects routes offered by the
staging catalog and requires a recorded shell-tool result. Set `OLLAMA_SMOKE_CASE` to one pytest
case ID to narrow it, or `OLLAMA_SMOKE_HOLD_SECONDS` to retain the Sandbox briefly for diagnosis
(the test caps this at 30 seconds). Each target writes evidence under its `test.outputs/` directory;
a rerun replaces local outputs, so save evidence first if needed.

Use the first failure to choose the next investigation:

| Observation                              | Likely seam                                                           |
| ---------------------------------------- | --------------------------------------------------------------------- |
| No `systest-*` Sandbox appears           | Bazel client, repository rules, kubeconfig or token setup             |
| Sandbox never becomes ready              | Scheduling, image pull, runner bootstrap or staging capacity          |
| App rejects the first API request        | Token audience, subject allowlist or app ingress                      |
| Turn hangs and decision history is empty | Sandbox proxy variables, proxy route or model ingress                 |
| Egress history denies an expected host   | Policy, binding or destination URL                                    |
| Assertions pass but teardown fails       | Cleanup or reconciliation; inspect the named Sandbox before rerunning |

Keep test output with proxy and app decision evidence. The transcript explains what the agent
attempted; decision history is authoritative for what the proxy served. Teardown watches the live
inventory until the created Sandbox's exact UID has effective `Suspended` mode before requesting
deletion. It rejects a replacement UID and does not reconnect a dropped watch; a delete acknowledgment
does not prove deletion completed ([`app_sandbox.py`](driver/app_sandbox.py)). After teardown, confirm no
`systest-*` Sandboxes remain in the selected namespace (the command below uses the default):

```bash
kubectl -n agentplane-staging get sandboxes.agentplane.allegedly.works
```

A killed test process cannot run fixture teardown. The fixture cleans up Sandboxes it created, but
a name prefix alone is not safe for an automated sweep because a user could choose the same prefix.
Add a suite-owned label or namespace before implementing that cleanup.

The [Firecracker CI suite](firecracker/README.md) runs `//agentplane/system_tests/suites:test_firecracker` on the dedicated KVM worker. It verifies same-Pod RAM and native process continuity through suspend/resume with streamed Runtime credential renewal. Cilium enforcement is outside this bare-k3s fixture.
