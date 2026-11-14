# Native service contracts

Ordinary CI contracts composing the production Go Gateway/Dispatcher and History binaries,
Runtime in its PID1 container, PostgreSQL, and the existing envtest Kubernetes API:

```sh
bbr test //agentplane/x/native_contracts:test_recovery --nocache_test_results
bbr test //agentplane/x/native_contracts:test_admission_reply_loss --nocache_test_results
```

The four Claude cases exercise:

- Completed-turn Runtime outage: Gateway reports `UNAVAILABLE`, History remains readable,
  the same journal restores the Session, Dispatcher returns the original receipt, and new input completes.
- Direct Runtime admission: the ordinary client is denied; the Gateway service identity submits
  directly through the proxy, bypassing Dispatcher receipt replay. Identical requests before and
  after restart return the original admission; changed payloads and operations are rejected.
  A subsequent Dispatcher input completes with exactly two recorded admissions and completions.
- Active Runtime crash: hold an actual model response, SIGKILL PID1 and observe its harness fence,
  retain the History prefix, reconcile the interrupted turn, and complete a distinct next input
  through Dispatcher without duplicate admission or confirmation.
- History outage: suspend the actual serving process while Runtime completes a turn, observe
  reader unavailability, then resume and verify journal catch-up and a contiguous cursor-resumed
  stream containing exactly the retained prefix and new entries. A distinct next input then
  completes with three admissions, model requests and completions and the original prefix intact.

These complement owner tests, including Runtime journal/admission, the native adapter restart
matrix, Gateway incarnation fencing, History ingress ordering and service authorization. Direct
Runtime retry is distinct from the separate admission-reply-loss contract: one Dispatcher
submission causes two authenticated Runtime handler calls, equal real admission responses and
one History admission, followed by a successful next input. It uses the same Runtime and CLI
startup code in a test-only binary whose interceptor returns `UNAVAILABLE` after the first real
handler commits. The four recovery cases retain the production executable. The fault models a
discarded admission response, not a socket reset. A later Dispatcher receipt replay must cause
no third Runtime call. Rejecting a valid journal replay as a conflict makes this contract fail.

Suspending History tests unavailability; it does not establish a History process restart,
database failover or packaged Deployment rollout.

The provider and model configuration are scripted. API requests, TokenReview, workload
certificates, service transports, databases and the pinned native harness are real. Sandbox,
WorkloadCredential and Pod status are authored by the fixture; no controller, kubelet,
scheduler, Cilium, browser or app runs. PostgreSQL, Runtime's Debian base and Ryuk remain OCI
inputs; the service OCI graph and K3s are absent. Fresh case-owned databases, API namespaces,
credentials, homes and journals isolate tests; only intentional in-case restarts reuse a journal.

The package lives outside `system_tests/` to avoid its inherited app fixture dependency.
These ordinary targets add coverage while the deployed gate remains required. Their presence
alone does not prove existing deployment tests redundant or qualify a smaller graph.

An opt-in control discards only the completed-turn case's Runtime state before restart. It must
fail at retained Session inventory, rather than setup:

```sh
bbr test //agentplane/x/native_contracts:test_recovery --nocache_test_results \
  --test_arg=-k=test_runtime_transport_outage \
  --test_env=NATIVE_CONTRACT_DISCARD_RUNTIME_JOURNAL=1
```

Measured native action time is composition feasibility evidence. Complete-CI savings and low
flake rates require the pinned graph comparisons and exposure tracked in
[#16099](https://github.com/agentydragon/ducktape/issues/16099).
