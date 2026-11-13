# Packaged guest Session contract

The manual `//agentplane/x/direct_vm_boot:test_runtime_session` now runs two real
packaged Claude Shell turns through Dispatcher, Gateway, Runtime and Go History.
The first writes a fresh nonce to a case-owned workspace; the second reads the
same file. The test requires the two original command IDs, sourced contiguous
History, two completed turns, a ready Session and one unchanged Runtime boot ID.
The kernel, initramfs, guest root, Runtime disk and systemd units are current real
fixture artifacts. External model responses are scripted.

`runtime_guest.py` extracts the existing authenticated-guest launcher for the
Gateway-only and Session targets. Both retain a fresh Sandbox/Pod/boot/state
identity, fixture-issued mTLS, real envtest and PostgreSQL, state-owner restoration,
container removal and a free host-forward listener. The shared model-config and
CONNECT fixtures default to loopback as before; this guest opts into a slirp host
address and the tunnel still accepts only its exact configured authority.

The positive uses end-to-end TLS directly to the real History listener. The
opt-in wrong-History control adds a TLS relay that requires the Runtime client
certificate, validates the real upstream's CA and localhost DNS, and presents a
same-CA localhost leaf with only a different SPIFFE URI. Before the guest starts,
a real Health.Check through both TLS legs must return SERVING. Runtime continues
to require the canonical History URI. Healthy Gateway readiness and the real Shell
operation precede failure of the unchanged History-completion assertion; serial
logs must show rejection of `spiffe://agentplane/egress-proxy`.

The relay owns its connections and tasks. A two-second TLS close bound forcibly
aborts the transport if the peer does not complete shutdown. This applies only
to the opt-in negative control.

## Physical evidence and failures

All invocations below are native Bazel children; no case is a cached pass.
These are implementation attempts, not a reliability cohort or matched timing
comparison. Controls that deliberately fail are distinct from healthy failures.

| Native invocation                      | Result and what changed afterward                                                                                                                                                                                                             |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `5aee53ed-f780-4d04-914d-6e4a82645f40` | Analysis/aspect failure: missing direct protobuf dependency on the extracted helper. Zero test bodies; fixed dependency.                                                                                                                      |
| `86c56ec9-2358-4767-b5a3-3145f2591bef` | Two healthy failures: Session cwd incorrectly equalled the workspace root; Gateway teardown raced the first serial lifecycle record. Use a case-owned child directory and require the record before teardown.                                 |
| `57e74ef1-4b10-4c01-9b70-28065dd55a0f` | Two healthy failures: a Session is correctly dormant before first input, and Docker/QEMU loading consumed RPC readiness time before the serial device opened. Remove premature ready assertion and give launcher startup its own phase.       |
| `449d966e-ea13-48de-b12f-686a7876aa23` | Gateway passes; Session Shell returns the right nonce plus CLI metadata, then a whole-text assertion fails and leaves the model exchange unsettled. Require the exact first line, non-error result and matching tool-call ID before replying. |
| `97ed514e-b133-431b-b2f3-4019ef8a951b` | Session passes both real turns and cleanup.                                                                                                                                                                                                   |
| `25746e94-c5ff-47c3-8a35-fd8d59cbb497` | Invalid negative control: Go History rejects its own altered local identity during fixture startup. This does not prove guest rejection; replace with the healthy-service relay.                                                              |
| `8ec70561-b98e-4349-8645-416d6f65243f` | Gateway passes; Session's 30-second RPC deadline expires while Linux is still doing mounts/systemd startup. Serial contains no Runtime process record. Move RPC readiness after actual packaged Runtime startup under the existing QEMU cap.  |
| `c2191426-029b-4e82-88a8-d9a5d936ca8c` | Valid wrong-URI control: Health.Check SERVING, authenticated Runtime, real Shell response, explicit unexpected-SPIFFE-ID rejection, then empty History fails turn-completion. Cleanup passes.                                                 |
| `680b53ca-b068-4e8c-bc07-4fdd8372bf40` | Both healthy targets pass: two NEW physical actions, two cases, no failures/errors/skips. Gateway process 101.894s and CPU 172.990s; Session process 163.792s and CPU 356.143s. Bazel elapsed 243.426s.                                       |
| `9077776c-c243-4fc1-98b6-bac1e841d925` | Same valid wrong-URI rejection and intended History assertion after the startup phase change. One NEW action, one expected failing case, no fixture error/skip.                                                                               |
| `bfd90ef2-08e3-4e49-9ba4-a915ccff0d05` | Final control-only transport-abort cleanup check: real Health.Check SERVING, explicit wrong-URI rejection and the intended History assertion. One NEW expected failing case, no fixture error/skip; owner restoration and removal verified.   |

The QEMU process retains its existing 120-second cap. Docker/QEMU launch has a
30-second bound; actual Linux/systemd boot is bounded by that QEMU lifetime.
The authenticated-RPC 30-second deadline begins after the packaged Runtime emits
a serial record. This separates launch, boot and RPC readiness; it does not extend
production deadlines or skip guest failure. The manual target's aggregate history
query returned no rows, so timings above use its exact native REAPI records and
JUnit rather than inferring a population distribution.

## Coverage and next measurement

These contracts do not prove real controller reconciliation, certificate issuance,
Secret projection, KubeVirt scheduling/lifecycle, PVC behavior, App routing, Codex
inside the guest, or executor resource enforcement. The Session fixture explicitly
disables its executor budget. Existing deployed VM and Firecracker acceptance and
all required tests remain in place; both new Runtime targets remain manual.

The direct guest now supplies a packaged-harness witness without the cluster
stack. It has not established a complete-feedback saving, a reliable failure rate,
or permission to replace those deployed guarantees. The next useful performance
probe should measure whether copying all four read-only guest artifacts before
QEMU needlessly forces input transfer: compare direct read-only mounts with the
same tests, assertions, platform and cache exposure before changing the launcher.
