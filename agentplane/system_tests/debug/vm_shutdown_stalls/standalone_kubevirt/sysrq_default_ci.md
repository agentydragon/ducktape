# Default SysRq capture for VM CI

The VM system-test runtime enables the guest's shutdown SysRq worker by default;
`AGENTPLANE_VM_SHUTDOWN_SYSRQ=0` disables it for a test action. This configures
test VM templates, not production VMs. The worker waits 18 guest seconds after
the watchdog is notified of ACPI and requests one `w` dump only if the proof
conditions have not been marked complete. Its proof check and request can race.
The guest kernel, worker and serial output must still make progress.

The delay is a tradeoff: an earlier 12-second full-suite trial dumped on a
guest that subsequently shut down successfully; a later delay risks missing
the dump before the host's 30-second grace expires. Guest and host clocks and
console delivery are distinct. Neither experiment establishes a generally safe
threshold or proves that a dump cannot perturb shutdown. For the 12-second
results and normal shutdown timing, see [sysrq_capture.md](sysrq_capture.md).

## Validation on 2026-11-07

The tested change was commit `532ee07de2`, based on `18b925bb3042`.

- The [focused invocation `4a5a2698-cd0d-4d0e-bc2f-022e894051b8`](https://app.buildbuddy.io/invocation/4a5a2698-cd0d-4d0e-bc2f-022e894051b8)
  passed `test_runtime` and `test_shutdown_sysrq`, including default selection,
  opt-out and the proof-gated worker test.
- The [uncached full-suite invocation `6e3c26f6-87d3-467e-a3ce-e8613df556ca`](https://app.buildbuddy.io/invocation/6e3c26f6-87d3-467e-a3ce-e8613df556ca)
  passed without setting the opt-in variable. All three retained guest CRI
  consoles showed the boot argument, one 18-second arm marker, proof and
  powerdown, with no SysRq begin marker. Request-to-proof receipt intervals
  were 5.063, 5.957 and 7.874 seconds. The VMI UIDs were
  `17919b17-8816-4e25-a0ed-a70f7116d32f`,
  `1db7d396-6917-421f-9fe2-2ed66633e3cc` and
  `892c8c74-94f3-4a69-9c22-1918a5987cde`.
- The [policy-off direct-VMI invocation `4f2916c5-28e3-4137-8f4d-71cabb594c6b`](https://app.buildbuddy.io/invocation/4f2916c5-28e3-4137-8f4d-71cabb594c6b)
  intentionally stopped the virtiofs sidecar and failed the missing-proof
  assertion. VMI UID `b0b51921-690a-4d73-bcc1-988f350a1c49` emitted the arm
  at 20:31:22.564 UTC, SysRq begin at 20:31:40.581, kernel blocked-task and
  `fuse_sync_fs` stacks, and successful end at 20:31:40.746. Its UID-correlated
  host grace-expiry record was at 20:31:52.468, about 11.7 wall-clock seconds
  after the end marker. Serial lines interleaved with other shutdown logging.

The positive control demonstrates capture before teardown in one deliberately
induced FUSE stall. It does not reproduce the historical CI stall or guarantee
the same timing when an actual failure occurs. Raw logs remain available through
the BuildBuddy artifacts; this note does not copy them.
