# One-guest shutdown diagnostic

Run the repeatable guest teardown check with:

```sh
bbr test //agentplane/system_tests/suites:test_vm_shutdown_diagnostic --nocache_test_results --test_output=errors
```

To opt this single manual diagnostic into passive host virtiofsd snapshots, use:

```sh
bbr test //agentplane/system_tests/suites:test_vm_shutdown_diagnostic \
  --test_tag_filters=manual \
  --test_env=AGENTPLANE_VM_VIRTIOFSD_TIMELINE=1 \
  --nocache_test_results --test_output=errors
```

The sampler takes one baseline after Sandbox readiness and before `sandbox_scope` starts cleanup. It uses the readiness-projected launcher Pod name, namespace and UID, checks that the UID equals `runtime_incarnation_uid`, and relies on the existing readiness topology check to verify that exact Pod and its VMI owner. The UID is the launcher Pod incarnation, not the VMI UID. From the projected Pod's ordinary and init-container status lists, it keeps only sanitized `virtiofs-*` names and full runtime container IDs; each sampled host process is correlated to the ID in its UID-matched cgroup. Missing, ambiguous or unmapped IDs are labeled explicitly.

After the raw serial stream shows the ACPI request marker, a background observer takes at most 18 snapshots, spaced two seconds apart, for no more than 36 seconds. Before ACPI, one absolute 600-second deadline covers waiting for the baseline, serial directory and request marker. Each process scan has a 0.75-second budget, keeps at most six matching daemons and 48 threads, and each output file is capped at 512 KiB. That bounds sample storage to 9.5 MiB including the ready-time baseline. The observer stops early only after the guest completion marker and two complete scans with no matching daemon. Incomplete or truncated scans do not count as absence.

This opt-in path reads only host `/proc` and retained serial files; it does not issue QMP commands. It does not change the default observer or other suites. A matching process snapshot is not proof that virtiofsd was servicing guest requests, and a baseline with no matching process is recorded as such rather than evidence of service health. Baseline/observer failures are retained under `vm-artifacts/host-qemu/` without changing guest shutdown behavior.

The test creates one `environment-vm` Sandbox through the normal app API, waits for guest readiness, then deletes it through [`sandbox_scope`](../../driver/sandbox.py)'s ordinary cleanup/finalizer path. That path sends ACPI and lets the guest's existing systemd shutdown units stop the runner and unmount `/state/workspaces` before `/state`. The test does not start a Session or harness and creates no concurrent guests.

It verifies the guest's ACPI handler receipt marker, the completion marker emitted after both storage mounts are gone, and the final kernel `reboot: Power down` line, all in order in one raw serial stream. The retained report includes those exact lines, VMI UID, UID-correlated virt-handler and virt-launcher records, and artifact paths. It classifies missing host logs and missing positive host shutdown events separately. It treats the observed grace-expired, launcher kill, or stopped/destroyed domain events as forced-stop evidence; it does not infer that shutdown was orderly from guest markers alone. Progress phases separate setup failure, launch-before-readiness evidence, incomplete deletion and post-delete evidence.

This is a one-guest version of the actual CI VM fixture, not a minimal KubeVirt deployment. `stack="vm"` still starts the complete rendered Agentplane application and PKI/egress services in a disposable k3s cluster. It retains the CI guest image, KubeVirt, Kyverno admission, virtiofs `/state` and the separate nested ext4 workspace disk at `/state/workspaces`, as declared in the [guest mount table](../../vm/guest/fstab). The guest's [ACPI handler](../../vm/guest/acpi_poweroff) and [unmount proof](../../vm/guest/shutdown_complete) remain on the real shutdown path. The standalone target avoids session/harness work and overlapping guests, but it does not yet avoid full-stack setup cost. Creating a bare VMI directly would bypass or need to rebuild Sandbox-controller wiring and the ordinary deletion path, so this target is an intermediate diagnostic rather than the requested cheaper outside-VM reproduction.

The report is written to `vm-artifacts/shutdown-diagnostic.json` under Bazel's undeclared test outputs. Raw serial, launcher, handler, virtiofs, native journal and host-QEMU artifacts remain alongside it. On a test failure, diagnostic-writing errors are logged while the original test exception is preserved.

The host-message matchers were checked against the retained late-failure logs from [invocation `15e9310a-7352-56a3-87c6-322dfb3065c0`](https://app.buildbuddy.io/invocation/15e9310a-7352-56a3-87c6-322dfb3065c0): the handler's grace-expired message carries the VMI UID, and the matching launcher records `Signaled vmi kill` and `Domain event="stopped" detail="destroyed"`. The destroyed-domain event is itself classified as forced-stop evidence; in the retained incident it accompanies the grace-expired and kill messages. A successful guest in the same handler logs reached `Shutoff reason Shutdown`, without the destroyed-domain event. The adjacent [run6 artifacts](README.md#observed-failure) show a launch that stopped with `detail="failed"` and `Crashed` before shutdown; the diagnostic records such launcher output as launch evidence, not proof of a failed boot or a forced shutdown. Host logs are correlated by VMI UID because handler logs retain older VMIs and names can be reused. A missing ACPI marker means receipt was not observed in retained guest output; it does not prove ACPI was never delivered.

## Observed run

On base `fcfff5f4deae44d5695d9530dc5471a4e2943e3d` plus the guest workflow, [BuildBuddy invocation `1562e2ef-810d-40f8-9d68-57fc07085727`](https://app.buildbuddy.io/invocation/1562e2ef-810d-40f8-9d68-57fc07085727) passed 1/1 test in 292.0 seconds. Its report classified `orderly_guest_powerdown` after `sandbox_deleted` for one VMI (`90779e07-3ea5-4518-a771-a31e74179874`). The raw serial contained four ACPI receipt lines, one unmount-complete marker and `[   68.679721] reboot: Power down`, in order. UID-correlated handler and launcher logs were present; the handler reported `Domain is in state Shutoff reason Shutdown`, with no grace-expired, `Signaled vmi kill`, or stopped/destroyed records. This invocation preceded the final addition that treats stopped/destroyed itself as forced-stop evidence; the recorded event set had no such event, and focused tests now cover the final classifier.

This healthy execution shows that the target captured the expected evidence once. It neither reproduces nor rules out the intermittent shutdown stall.

The opt-in host-process timeline was smoke-tested separately in [invocation `710a9ea0-2255-495a-9142-0b8896069498`](https://app.buildbuddy.io/invocation/710a9ea0-2255-495a-9142-0b8896069498); the mapped daemon identities and raw snapshots are in [passive_virtiofsd_timeline/README.md](passive_virtiofsd_timeline/README.md). This was one healthy guest, not a stall reproduction.
