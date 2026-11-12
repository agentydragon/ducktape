# Agentplane system tests

These tests exercise a running Agentplane deployment: they create Sandboxes through the app,
run Sessions on the harnesses, and check cross-component behavior and retained evidence. They
cover deployment behavior beyond what unit tests can establish; the egress contract is described
in [the egress spec](../egress/SPEC.md).

## Where things live

| Path                                    | Owns                                                      |
| --------------------------------------- | --------------------------------------------------------- |
| [`suites/`](suites/)                    | CI, manual and live test entrypoints                      |
| [`scenarios/`](scenarios/)              | Behaviors and assertions against a deployment             |
| [`driver/`](driver/)                    | Session, Sandbox, API, model and concurrency helpers      |
| [`deployment/`](deployment/README.md)   | Fixture charts, workloads and stack lifecycle             |
| [`k3s/`](k3s/README.md)                 | Retained test node, bootstrap, networking and suite macro |
| [`firecracker/`](firecracker/README.md) | Firecracker lifecycle adapter and KVM acceptance          |
| [`vm/`](vm/README.md)                   | VM runtime, storage, guest image and evidence             |

Offline tests stay beside the helper they exercise. The deployment guide owns stack setup and
artifacts; the VM guide owns guest details. The suite BUILD files and test modules are the source
of truth for exact case membership.

## Suite entrypoints

Each `k3s_suite` target starts one fixture stack. Scenarios share it only when their resource
changes are compatible. `test_dispatcher` owns a separate stack because it scales the Runner Gateway
and deletes a controller Pod. The external MCP delegation acceptance owns another stack because it
scales the app to zero after OAuth consent.

| Target                                                                 | Coverage                                              |
| ---------------------------------------------------------------------- | ----------------------------------------------------- |
| `//agentplane/system_tests/suites:test_pod`                            | Pod Sandbox scenarios                                 |
| `//agentplane/system_tests/suites:test_session_deletion`               | Session deletion and replacement                      |
| `//agentplane/system_tests/suites:test_dispatcher`                     | Dispatcher queue, wake and replicas                   |
| `//agentplane/system_tests/suites/mcp:test_actions`                    | MCP Actions and operator decisions                    |
| `//agentplane/system_tests/suites/mcp:test_services`                   | MCP services and central sampler                      |
| `//agentplane/system_tests/suites/mcp:test_sampler_roll`               | Sampler replacement with live executor work           |
| `//agentplane/system_tests/suites/mcp:test_gateway_crash`              | Abrupt Gateway loss during a live executor read       |
| `//agentplane/system_tests/suites/mcp:test_history_outage`             | History outage during an active central-sampler turn  |
| `//agentplane/system_tests/suites/mcp:test_delegation`                 | External MCP delegation                               |
| `//agentplane/system_tests/suites/mcp:test_parent_delegation`          | Parent-to-child Session CLI flow                      |
| `//agentplane/system_tests/suites:test_vm`                             | VM Sandbox scenarios on the software-emulated stack   |
| `//agentplane/system_tests/suites:test_real_model`                     | Real-model egress and Session instructions            |
| `//agentplane/system_tests/suites:test_vm_manual`                      | Manual VM storage, ownership and suspend/resume cases |
| `//agentplane/system_tests/suites:test_vm_profile`                     | Manual profiling variant                              |
| [`//agentplane/system_tests/x/vm_kvm:test_vm_kvm`](x/vm_kvm/README.md) | Experimental KVM variant, manual                      |

`test_vm_manual`, `test_vm_kvm` and `test_vm_profile` carry the `manual` tag. The staging checks
[`test_thread_latency`](suites/live/test_thread_latency.py) and
[`test_ollama_routes`](suites/live/test_ollama_routes.py) carry both `manual` and `no-remote-exec`;
the [live-test instructions](#running-live) explain how to run them.

For current test names and arguments, inspect the suite modules, such as
[`test_pod.py`](suites/test_pod.py), [`test_dispatcher.py`](suites/test_dispatcher.py), and
[`test_actions.py`](suites/mcp/test_actions.py). Scenario modules are libraries called by suite
entrypoints; helper unit tests live beside their implementation. Use the [deployment
README](deployment/README.md) for stack behavior and the [VM Sandboxes guide](deployment/README.md#vm-sandboxes)
for VM image layout and lifecycle.

## Behavior-specific anchors

- [`thread_stream.py`](scenarios/thread_stream.py) checks archived event IDs, reconnect from
  `Last-Event-ID`, command identity, and turns that complete without a watching tab. History can
  settle streamed deltas, so reads need not contain every cursor another tab observed.
- [`suspend_resume.py`](scenarios/suspend_resume.py) checks that a new Sandbox incarnation keeps
  the same storage and archived event prefix while native Sessions continue. Restart and attach
  failure boundaries belong to `//agentplane/env/e2e:test_restart` and `//agentplane/env/e2e:test_attach`.
- The Pod suite also checks explicit resume after idle suspension: the Sandbox UID stays fixed, its
  Pod is replaced, and a prior failed turn remains retained before a new command is sent.
- `test_delegation` continues an external MCP Connection with the app scaled down; the separate
  `test_parent_delegation` case drives a child Session through the actual `agentplane` CLI.
- MCP operator login reads one named test Secret from the fixture API and keeps credentials in
  memory. The caller suppresses HTTP logging and refuses pytest `--showlocals` for the full exchange;
  do not add credential-bearing traces or artifacts ([`test_actions.py`](suites/mcp/test_actions.py),
  [`operator_login.py`](driver/operator_login.py)).
- `Agent.run` returns only after a completed turn. Non-completed statuses and a failed command or
  harness launch stop the case before assertions about tool effects; it does not replay admitted input.
- Pod cases may reuse a pair of protocol runners, but each case creates fresh Sessions, HTTP streams
  and model-request queues. Queue checks run when a case ends; a failed or cancelled lease destroys
  the pair. Workspace storage runs alone before the pool is used ([`test_pod.py`](suites/test_pod.py),
  [`pod_runners.py`](suites/pod_runners.py), [`pod_cases.py`](suites/pod_cases.py)).
- Test markers must agree with the selected stack backend; deployment setup rejects scripted-model
  cases on the real-model stack and vice versa ([`system_test_target.py`](deployment/system_test_target.py)).

### Abrupt Gateway loss

`test_gateway_crash` must kill the exact Gateway process without orderly Lease release. Killing
before scale-down allows kubelet to restart the container in the same Pod; the restarted Gateway
can claim a new epoch and gracefully release it on scale-down. Kubernetes Pod status can still
show the original container ID after the replacement has claimed the Lease.

The fixture sends `SIGSTOP`, confirms the captured process is stopped, scales the Deployment to
zero, waits for that Pod's deletion timestamp and pending `SIGTERM`, then sends `SIGKILL` to the same
container ID. The pending signal shows kubelet has begun termination; the API timestamp alone
cannot. An open process status file guards against PID reuse, and cleanup kills the stopped
process even when an intermediate step fails. The original Lease UID, holder, epoch and
no-ready-endpoint assertions remain required.

The `gateway-crash-{before,process,terminating,deleted}.json` undeclared outputs retain the process
and Lease phases. Read runtime logs alongside Pod status to identify restarts. The
[old-ordering control](https://app.buildbuddy.io/invocation/28f0f32f-62f4-4055-be66-ea3f60d885b7)
reproduced the missing holder after epoch `1` became `2`; the
[repaired control](https://app.buildbuddy.io/invocation/17352c08-b9bb-4bd6-9676-32be63a8b4bb)
retained epoch `1` through Pod deletion, with `SIGTERM` pending on the stopped original process,
and passed the active read, exactly-once mutation and next-input checks. The
[final helper CI run](https://app.buildbuddy.io/invocation/c6a1a753-6505-5820-9de8-9f0e39ab8372)
also passed this scenario and retained both stopped and pending-signal process states.

## Model and egress evidence

Model-driven scenarios use either a scripted model served by the scenario or the real-provider
`test_real_model` stack. A scripted result demonstrates the harness-to-scenario path, not external
provider acceptance. The real standing-instructions case uses a marker absent from its prompt and a
control Session with the same prompt but no instructions; the instructed reply must contain the
marker and the control reply must not ([`session_instructions.py`](scenarios/session_instructions.py)). Real-model tests receive
`OPENAI_API_KEY` as a BuildBuddy test secret and carry the `live_openai_api` tag; do not print or
save credentials ([`defs.bzl`](k3s/defs.bzl)).

For proxy behavior, compare the agent's report with the Egress decision history. The report says
what the agent claims to have done; a proxy decision records what the proxy admitted. The egress
scenario checks both, including a private ConfigMap read through the Sandbox's own ServiceAccount
and a denied request to an unlisted host ([`egress.py`](scenarios/egress.py)). For absence checks,
require an executed command's explicit verdict; a model refusing to print a secret is not proof of
absence. The egress case checks the random ConfigMap value as well as proxy decision history.

## Central sampler

The k3s case checks the deployed Dispatcher, Gateway, LLM ingress and authenticated History path
together. Its setup and requested shell command must reach the same executor workspace
([`central_sampler.py`](scenarios/central_sampler.py)); the [service guide](../central_sampler/README.md)
covers the image and runtime contract.

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

## Dispatcher queue and replicas

The queue case leaves a stopped Sandbox unchanged. The replica case raises Gateway replicas to two,
removes the recorded lease owner and verifies durable queue recovery through its peer; opted-in app
and Notification submissions share a wake generation ([`dispatcher.py`](scenarios/dispatcher.py)).
The [Dispatcher guide](../runner_gateway/dispatcher/README.md) owns the command delivery contract.

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
effective suspension, delete acceptance and actual deletion ([`sandbox.py`](driver/sandbox.py)). See the
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
`agentplane-staging`. `test_thread_latency` mints a short-lived `agentplane-agent` token from the
caller kubeconfig unless `AGENTPLANE_SYSTEM_TEST_TOKEN` is supplied. The caller needs RBAC to create
tokens for that ServiceAccount. `test_ollama_routes` reads the deployed catalog and uses the same
caller kubeconfig for Sandbox operations. Do not print or save credentials.

| Variable                                 | Default                                                |
| ---------------------------------------- | ------------------------------------------------------ |
| `AGENTPLANE_SYSTEM_TEST_URL`             | `https://agentplane-staging.allegedly.works`           |
| `AGENTPLANE_SYSTEM_TEST_TOKEN`           | Minted from caller kubeconfig by `test_thread_latency` |
| `AGENTPLANE_SYSTEM_TEST_NAMESPACE`       | `agentplane-staging`                                   |
| `AGENTPLANE_SYSTEM_TEST_SERVICE_ACCOUNT` | `agentplane-agent`                                     |

Check the local seams before a long run:

```bash
command -v bazelisk kubectl
bazelisk version
kubectl config current-context
kubectl -n agentplane-staging create token agentplane-agent \
  --audience=agentplane --duration=600s >/dev/null
```

The preflight must not print or save the returned token. If it fails, fix caller RBAC or kubeconfig
before running the test. A Bazel module-loading failure is a caller's Bazel/repository-rule issue,
not a staging failure.

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
does not prove deletion completed ([`sandbox.py`](driver/sandbox.py)). After teardown, confirm no
`systest-*` Sandboxes remain in the selected namespace (the command below uses the default):

```bash
kubectl -n agentplane-staging get sandboxes.agentplane.allegedly.works
```

A killed test process cannot run fixture teardown. The fixture cleans up Sandboxes it created, but
a name prefix alone is not safe for an automated sweep because a user could choose the same prefix.
Add a suite-owned label or namespace before implementing that cleanup.

The [Firecracker CI suite](firecracker/README.md) runs `//agentplane/system_tests/suites:test_firecracker` on the dedicated KVM worker. It verifies same-Pod RAM and native process continuity through suspend/resume with streamed Runtime credential renewal. Cilium enforcement is outside this bare-k3s fixture.
