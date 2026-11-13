# Native service contract pilot

A manual experiment composing the production Go Gateway/Dispatcher and History binaries, a
production Runtime in its PID1 container, PostgreSQL, and the existing envtest Kubernetes API.
Run explicitly on RBE:

```sh
bbr test //agentplane/x/native_contracts:test_recovery --nocache_test_results
```

The single Claude case completes a turn, removes the Runtime listener, checks Gateway
`UNAVAILABLE` and retained History, restores the same Runtime state, replays the durable Dispatcher
receipt, and completes the next input. History must contain exactly two admissions and two turn
completions. Receipt replay can be answered by Dispatcher; this does not isolate Runtime deduplication.
The outage begins after a completed turn, so this does not test interrupted active work or a crash.

The upstream provider and model configuration are scripted. The Kubernetes API, TokenReview,
workload certificates, service transports, databases and native harness are real. Sandbox,
WorkloadCredential and Pod status are authored by the existing fixture; no controller, kubelet,
scheduler, Cilium, deployment rollout, browser or app is exercised. PostgreSQL, Runtime's Debian
base and Ryuk remain OCI inputs; the service OCI graph and K3s are absent.

The package lives outside `system_tests/` because its inherited `conftest.py` imports the app and
full scenario drivers. This pilot has no app dependency and changes no required CI gate.

An opt-in fixture fault discards only this test's Runtime state before restart. It must fail the
same contract; it is not a historical production mutation:

```sh
bbr test //agentplane/x/native_contracts:test_recovery --nocache_test_results \
  --test_env=NATIVE_CONTRACT_DISCARD_RUNTIME_JOURNAL=1
```

Run time is feasibility evidence for this narrow composition, not measured savings for existing
system tests or evidence that their coverage can be removed.
