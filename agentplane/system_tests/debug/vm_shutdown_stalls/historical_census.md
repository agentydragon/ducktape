# Historical VM shutdown failure census

This is a symptom census, not a count of a single diagnosed root cause. A guest is classified
below as a post-ACPI forced shutdown when its own retained console shows an ACPI poweroff
request but no shutdown-complete marker or final kernel powerdown, and the corresponding
launcher records a kill or a destroyed domain. Unmount progress is noted separately. None of
these observations, by itself, identifies where the guest was blocked or proves the same
mechanism as another case.

## Confirmed post-ACPI forced shutdowns in full VM tests

| Guest evidence (UTC date) | BuildBuddy child invocation                                                                                       | Population and observation                                                                                                                                                                                                |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-10-28                | [5f86d746-79d7-4c98-9ca9-b4d3f785943f](https://app.buildbuddy.io/invocation/5f86d746-79d7-4c98-9ca9-b4d3f785943f) | Focused predecessor test on `codex/session-load-retry`; both `/state/workspaces` and `/state` unmounted before the guest was killed. [Runner](https://app.buildbuddy.io/invocation/64e97ac8-9864-4feb-8caa-f9922c33ffb0). |
| 2026-10-30                | [ca76018e-1c98-5b86-9b03-ecba43ed672b](https://app.buildbuddy.io/invocation/ca76018e-1c98-5b86-9b03-ecba43ed672b) | Indexed devel CI; workspace unmounted, `/state` unmount not observed.                                                                                                                                                     |
| 2026-10-31                | [ed4d21fc-bdab-5f68-9570-2f675a8d6088](https://app.buildbuddy.io/invocation/ed4d21fc-bdab-5f68-9570-2f675a8d6088) | `fix/external-port-output-adapters` branch; both state mounts unmounted.                                                                                                                                                  |
| 2026-11-01                | [0cf2c912-5e0c-5ff1-86e8-56bf7115e5c2](https://app.buildbuddy.io/invocation/0cf2c912-5e0c-5ff1-86e8-56bf7115e5c2) | `agent/session-dispatcher-egress-proxy` branch; both state mounts unmounted.                                                                                                                                              |
| 2026-11-01                | [d4214eb4-024d-5202-afa0-e044b39e724d](https://app.buildbuddy.io/invocation/d4214eb4-024d-5202-afa0-e044b39e724d) | `agentplane/dispatcher-caller-privilege` branch; both state mounts unmounted.                                                                                                                                             |
| 2026-11-01                | [a484f1d2-410d-59dc-b647-e708e0e00738](https://app.buildbuddy.io/invocation/a484f1d2-410d-59dc-b647-e708e0e00738) | `agentplane/app-config-proto-http` branch; both state mounts unmounted.                                                                                                                                                   |
| 2026-11-01                | [be1b37be-14f1-5e99-ae9a-ad93d1dff5a0](https://app.buildbuddy.io/invocation/be1b37be-14f1-5e99-ae9a-ad93d1dff5a0) | `codex/agentplane-python-trim-b-policy-reader` branch; no state-unmount message was observed.                                                                                                                             |
| 2026-11-02                | [c2688816-1654-5f54-b6ba-9dfa0946a9d1](https://app.buildbuddy.io/invocation/c2688816-1654-5f54-b6ba-9dfa0946a9d1) | Indexed devel CI; workspace unmounted, `/state` unmount not observed.                                                                                                                                                     |
| 2026-11-06                | [15e9310a-7352-56a3-87c6-322dfb3065c0](https://app.buildbuddy.io/invocation/15e9310a-7352-56a3-87c6-322dfb3065c0) | `codex/cleanup-dd-dg` branch; both state mounts unmounted. The unchanged retry passed.                                                                                                                                    |

The November 6 instrumented [three-run experiment](https://app.buildbuddy.io/invocation/ac743375-b5cb-4109-aee8-67243e9ac78e)
also reproduced a post-ACPI forced shutdown in run 2. It is an investigation run with a
working-tree patch, so it is not added to the ordinary CI/branch count. See the [main
report](README.md) for its PID 1 observations and the limits of those observations.

## Related failures with different or uncertain explanations

- [d458ae44-3e48-4abc-95e2-b6c031a5cb80](https://app.buildbuddy.io/invocation/d458ae44-3e48-4abc-95e2-b6c031a5cb80),
  October 27: ACPI repeatedly attempted `poweroff`, but the guest logged `poweroff: No such file
or directory`; the domain was killed. This has a concrete command-availability failure and
  must not be counted as the later unidentified stall.
- [9887e50e-a25b-4df3-a8a2-d47f303469ae](https://app.buildbuddy.io/invocation/9887e50e-a25b-4df3-a8a2-d47f303469ae)
  and [58f790a6-5d13-4592-8360-68a57d0da653](https://app.buildbuddy.io/invocation/58f790a6-5d13-4592-8360-68a57d0da653),
  October 28: two focused, patched `agent/vm-guest-graceful-shutdown` runs saw repeated ACPI
  poweroff messages and were forcibly stopped, without the shutdown proof. The early shell-based guest
  shutdown script logged a signal to the runner and a wait for it; no state unmount was
  observed. These are experimental branch runs with a different guest shutdown implementation,
  not independent normal-CI measurements of the later systemd symptom.
- [a9f19f3d-93ee-5345-8470-33f12a264d05](https://app.buildbuddy.io/invocation/a9f19f3d-93ee-5345-8470-33f12a264d05),
  October 27: an early shell-based guest shutdown handler requested runner termination and waited
  for it until KubeVirt killed the domain. It is not evidence of the later systemd hang.
- [4789e905-f7c7-5cc3-b41a-796ae236dba4](https://app.buildbuddy.io/invocation/4789e905-f7c7-5cc3-b41a-796ae236dba4),
  [ee8b4d09-93de-5b36-bbb6-8aa03c2da3bb](https://app.buildbuddy.io/invocation/ee8b4d09-93de-5b36-bbb6-8aa03c2da3bb),
  and [b1090ce5-c5be-4b17-a2aa-9ce3c10ba059](https://app.buildbuddy.io/invocation/b1090ce5-c5be-4b17-a2aa-9ce3c10ba059)
  reported missing proof after QEMU's `iotlb_to_section` assertion during boot. They are not
  post-ACPI shutdown stalls.
- [1ad3292c-e695-5bfd-8dbe-7bd22d395aad](https://app.buildbuddy.io/invocation/1ad3292c-e695-5bfd-8dbe-7bd22d395aad)
  reported missing proof, but the affected guest's raw console and serial contain the proof
  marker concatenated with other console text and `reboot: Power down`. Do not count that
  receipt-parsing failure as a shutdown stall.
- [ab1d4ba3-ce97-45e9-b2bc-a1012c054a3a](https://app.buildbuddy.io/invocation/ab1d4ba3-ce97-45e9-b2bc-a1012c054a3a)
  was an opt-in native profile run with a missing proof receipt; the affected guest console
  showed `reboot: Power down`. Do not count it as a forced shutdown stall.
- Separate deliberately policy-disabled direct-VMI experiments, and the distinct pre-Ready
  RCU wedge, are described in the [main report](README.md); they are not normal-suite
  post-ACPI rate measurements.

## Coverage and interpretation

The search ran on 2026-11-07 UTC against `agentydragon/ducktape`, using BuildBuddy's
indexed histories for `//agentplane/system_tests/suites:test_vm` and its predecessor labels
`//agentplane/system_tests:test_vm_sandbox_vm` and
`//agentplane/acceptance:test_vm_sandbox_k3s_vm`, plus failed invocations updated on or after
October 21. The indexed histories had 1,166, 998, and 9 target rows respectively; many passing
rows are cached and are not fresh guest executions.

To find results outside those histories, an additional 556 failed invocations were selected by
CI role, missing runner association, or VM/Agentplane-related branch names. A further 1,440
unseen failed test invocations were selected from broad `remote run` or otherwise unlinked
runners. Their VM-named target outcomes were queried: 41 and 157 executed failed target
outcomes respectively were found, and their test logs were inspected. The exact queried IDs
and discovered outcomes are in [historical_census_manifest.json](historical_census_manifest.json).
A separate search selected 112 failed hosted runners explicitly naming VM targets; 84 had
matching failed-test logs and 28 had none available under that query. These searches overlap,
so counts across groups must not be added as independent runs. Matching launchers were
classified using their own console and launcher artifacts; runner and child IDs are linked
where relevant. Historical artifacts used the BuildBuddy legacy fallback when needed.

The search is not exhaustive: it excludes many narrowly targeted unrelated-looking runners,
may miss targets whose names do not contain `test_vm`, includes selected branch and manual
runs, and cannot classify a hidden shutdown failure when the relevant evidence was not
retained or the test failed earlier. It also does not deduplicate all shared or cached
executions. The previously suggested approximately 1% rate was based on two indexed cases
and a sampled cache denominator, omitting these branch and focused runs; it is not a reliable
estimate for the wider population. A defensible rate requires a predefined comparable
population, fresh physical execution IDs, and consistent per-guest shutdown diagnostics.
