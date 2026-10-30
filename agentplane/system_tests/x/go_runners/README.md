# Shared k3s runner spikes

Compare Go `testing` parallel subtests with Ginkgo 2 parallel specs. These are experiments;
the production suites still use pytest and TaskGroups. See the
[evaluation and measurements](../../debug/go_runner_spikes.md).

Both runners launch `:host_bin`, a Bazel Python executable which owns the existing k3s
stack and unchanged VM scenario bodies. Go owns case selection, scheduling, deadlines,
cleanup and reporting. This measures the runner integration before rewriting protocol
clients and assertions in another language. The host is test infrastructure, not a service
deployed into the stack.

Each case has its own Client, scripted upstream and Sandbox. Model requests route to that
case's scripted upstream using the Pod identity verified by LLM ingress. Cleanup cancels the
case, captures diagnostics, suspends/deletes its Sandbox, checks unsettled model requests,
and closes its Client. Stack teardown waits for every case's cleanup.

| Target suffix | Go `testing`                                            | Ginkgo 2                                            |
| ------------- | ------------------------------------------------------- | --------------------------------------------------- |
| `:probe`      | Six individually named subtests, at most three parallel | Six individually named specs across three processes |
| `:vm`         | Claude supervision, Codex turn, Codex tool OOM          | The same three scenario bodies                      |

The VM targets are `manual` so ordinary CI does not duplicate the production VM suite.
Both runners use `k3s_runtime("vm")` for the same images, manifests and RBE resources.
Each VM case boots independently. The exclusive phase checks that parallel case resources
have drained while the stack is still alive. Ginkgo requires its coordinator; directly
running `ginkgo:compiled.test` does not exercise its multiprocess scheduler.

```bash
bbr test //agentplane/system_tests/x/go_runners/native:probe \
  //agentplane/system_tests/x/go_runners/ginkgo:probe
bbr test //agentplane/system_tests/x/go_runners/native:vm \
  //agentplane/system_tests/x/go_runners/ginkgo:vm --nocache_test_results

bbr test //agentplane/system_tests/x/go_runners/native:probe \
  --test_filter=TestSharedStack/scenarios/a
bbr test //agentplane/system_tests/x/go_runners/ginkgo:probe \
  --test_filter='independent scenarios a$'
```

For the two fault probes, run both `:probe` targets with
`--test_env=RUNNER_SPIKE_FAULT=failure` or `--test_env=RUNNER_SPIKE_FAULT=timeout`.
These commands intentionally fail one case. The other five should pass, all six should
close, and the host should exit. Go uses a per-case context deadline; Ginkgo uses its native
`SpecTimeout`. Neither probe simulates an unkillable worker or a hard Bazel timeout.

Bazel artifacts include JUnit, `runner-events.json`, `stack-host.log`, existing k3s timeline
and VM phase spans, model requests, native histories and guest consoles. The lifecycle
artifact records stack identity and separate Sandbox/Pod UIDs; it adds no timing mechanism.
