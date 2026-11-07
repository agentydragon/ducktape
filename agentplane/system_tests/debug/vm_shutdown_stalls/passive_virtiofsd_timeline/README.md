# Passive host virtiofsd timeline

The opt-in full-stack shutdown diagnostic was run once on RBE in [BuildBuddy invocation `710a9ea0-2255-495a-9142-0b8896069498`](https://app.buildbuddy.io/invocation/710a9ea0-2255-495a-9142-0b8896069498). It passed 1/1 in 245.0 seconds, with one test run and one attempt. The source base was `13fc7233b2daecfb4d87a67e0e1c36da715a89e3` plus the RBE-synced source patch with SHA-256 `de0f2cd6095be899f357c5b509911b4d373f27fabe27776887274b11e814b4d6`. The exact command was:

```sh
bbr test //agentplane/system_tests/suites:test_vm_shutdown_diagnostic \
  --test_tag_filters=manual \
  --test_env=AGENTPLANE_VM_VIRTIOFSD_TIMELINE=1 \
  --nocache_test_results --test_output=errors
```

Only the explicitly opted-in one-guest diagnostic starts the passive timeline. The target remained in namespace `systest`, and the exact launcher Pod was `virt-launcher-vm-shutdown-diagnostic-7jg6c` with UID `2a3f6bdf-805a-4784-8c5e-5ef4ac7fba89`. The baseline at `2026-11-07T02:46:44.056Z` found three `virtiofsd` processes and nine threads. Each PID's full hexadecimal ID extracted from its cgroup matched the Kubernetes `containerID` from the exact projected Pod status:

| PID  | Container name                                  | Full container ID                                                  |
| ---- | ----------------------------------------------- | ------------------------------------------------------------------ |
| 3685 | `virtiofs-state`                                | `614e7766d77c9843dc76cc2e5af0179640458dd0ca7246dc8274b536f744d05a` |
| 3724 | `virtiofs-agentplane-environment-runtime-trust` | `2dd5bbc0bfc6292ebacac29c47f165676c2a40eba58e4d0f8c7e0b701599def9` |
| 3753 | `virtiofs-agentplane-environment-runtime-tls`   | `ebb9db52a29051aef00b1728586cf12064e3ec41f2adf80f6456a5fd192c45db` |

Shutdown samples 00–03, at two-second intervals from `02:46:45.725Z` through `02:46:51.725Z`, retained all three named processes. Samples 04 and 05 at `02:46:53.725Z` and `02:46:55.725Z` were complete scans with no matching process. The observer stopped after the guest completion proof and two complete scans with no matching daemon (`proof-and-daemon-absent`, 10.008 seconds after the ACPI marker). The diagnostic report classifies the guest as `orderly_guest_powerdown`: four ACPI receipt lines, completion proof, `reboot: Power down`, and a UID-correlated `Shutoff reason Shutdown` record, with no grace expiry, launcher kill or destroyed-domain event.

The saved host files are the raw bounded `/proc` snapshots and timeline metadata from that test, plus its shutdown summary JSON. [`SHA256SUMS`](run-710a9ea0/SHA256SUMS) covers each retained raw file. A case-insensitive scan found no credential-like strings; these snapshots do not record command lines or argv. The baseline contains the sanitized name-to-container-ID map, and each process line repeats only the matched full ID and container name. Missing, ambiguous or unmapped cgroup IDs are reported explicitly by the collector.

This is a healthy smoke of the observer, not a reproduction or exclusion of the intermittent stall. The sampled process and thread states are passive point-in-time observations; a matching process does not prove that virtiofsd was servicing guest FUSE requests. The scan begins after the ACPI marker and cannot show a pre-stop state after the process has exited. The default test path does not enable this observer or suppress its normal QMP snapshots.
