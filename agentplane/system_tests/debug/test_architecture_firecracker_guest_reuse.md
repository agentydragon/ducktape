# One fresh Firecracker guest for trust and live-state acceptance

The required `test_firecracker` creates a guest for live Session, certificate
rotation and RAM-preserving autosuspend/resume, then creates another guest for
Gateway certificate trust. The latter helper only opens authenticated TLS
connections and checks Ready; it does not alter the guest. In required invocation
[`acbad609`](https://app.buildbuddy.io/invocation/acbad609-9135-5b68-9fa5-30709c4690d4),
the second case spends about 32.2 seconds creating, readying and deleting its
guest, with about 1.35 seconds in the trust helper. These are retained span
durations, not a demonstrated optimization or a fleet estimate.

The manual `test_firecracker_shared_guest_candidate` runs that same helper on
the first fresh guest before creating a Session, rotating a credential or
suspending. It then calls the unchanged live-state scenario with that descriptor.
The required control still has two case-owned guests. Guest creation moves from
the scenario into its caller in both targets; no optional hooks or alternative
scenario implementations are introduced.

The trust helper keeps its positive Gateway SVID handshake, forged cluster-CA
leaf rejection and Ready assertion over the real Pod-IP route. The live-state
scenario keeps both model turns, mounts and home checks, live child progress,
certificate reissuance and serving checks, actual one-minute idle suspension,
checkpoint/reap and resume states, exact Pod/boot/Session/process identities,
and ordinary owned cleanup. This does not lower resource, timeout or duration
coverage, change pool placement or add a worker.

The tradeoff is one compound JUnit case instead of two independently clean guest
fixtures. Trust runs before the live-state mutations, but those TLS handshakes
now precede that scenario. Review the resulting live-state and cleanup evidence;
do not infer isolation equivalence merely from fewer case IDs.

## Bounded comparison

At one fixed source, run A then B serially, uncached and with one flaky attempt:

```sh
bbr test --config=ci //agentplane/system_tests/suites:test_firecracker \
  --nocache_test_results --flaky_test_attempts=1 --test_output=errors
bbr test --config=ci //agentplane/system_tests/suites:test_firecracker_shared_guest_candidate \
  --test_tag_filters=-live_openai_api \
  --nocache_test_results --flaky_test_attempts=1 --test_output=errors
```

Retain NEW physical identities, native result, all nonempty JUnit cases, KVM
queue/preparation/execution, compatible fixture state and image imports, case
and sandbox spans, continuation evidence, cleanup and CPU. A must execute its
two cases; B must execute its one compound case with the same two scenario
owners. Failures stay in the comparison. Repeat B/A only if the first pair
shows useful work removal with no new failure or cleanup signature.

A promising target result needs a same-source complete selected-graph comparison
with only this substitution. The previous required invocation's Firecracker
tail was 90.046 seconds beyond Pod, so the predicted 32.2 seconds can matter
there, while a VM-only or Services-only reduction cannot. Those fixed-clock
bounds do not predict a new queue or prove complete feedback savings.

Keep required selection unchanged until the result, isolation tradeoff and graph
benefit are reviewed. A clear result with retained assertions may be ported
independently; otherwise keep the experiment and its follow-up trigger in
[#16099](https://github.com/agentydragon/ducktape/issues/16099).
