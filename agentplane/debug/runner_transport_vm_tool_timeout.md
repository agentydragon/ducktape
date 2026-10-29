# VM forward tool-result timeout

During the runner-transport PR's integration with the sampler-Pod fixture, the forward VM
case timed out at the second `Endpoint.next()`, after setup and the first model response.
Both Pod modes and the reverse VM case passed.

- Failed execution: [bdc405a1](https://app.buildbuddy.io/invocation/bdc405a1-002e-49a8-9046-3503186733af).
  Hosted CI [1c07a35c](https://app.buildbuddy.io/invocation/1c07a35c-0c75-5909-9286-0a063089d005)
  returned the same VMI, Pod and session artifact IDs; these are one shared execution,
  not independent reproductions.
- The available logs do not establish a cause. Sampler EOF was logged during teardown,
  and the failure occurred outside the existing session-diagnostics capture boundary.
- Fresh diagnostic run [4c09d43d](https://app.buildbuddy.io/invocation/4c09d43d-b17f-4af3-8c5d-adbdba6c597c)
  passed both VM cases, with different workload IDs and zero failures or skips.
  No runtime behavior, assertions or timeouts changed.

`test_central_sampler.py` now captures sampler state, retained History, Sandbox/Lease state
and bounded control-service logs if the tool-result model request fails. The original
failure remains unexplained; the passing rerun does not establish a runtime fix.
