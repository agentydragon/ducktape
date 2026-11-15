# Direct Runtime guest image mounts

The manual Runtime guest launcher now binds its four declared image runfiles into
Docker read-only instead of copying them into the temporary input directory.
Kernel, initramfs, root and Runtime disks remain Bazel inputs. QEMU arguments,
fixture identities, test assertions and cleanup are unchanged. This removes an
eager local copy; it does not remove an input edge or prove fewer CAS bytes.

The A/B/B/A screen starts at `8a555b21141fded2166fcc17cd14096187e22842` and changes
only three launcher/consumer files. Candidate patch SHA-256:
`11d5b1ca5cf2346235da8279229def6087567297678f3b6b3bda5bbf9d96d0be`.
Harness SHA-256: `757ceeb36afdb3e81a4148d67d109c6853db6692999b1c65aa792f5b98d9298b`.
Each arm starts a separate hosted runner with normal action caching and forces
both test targets to execute once. All eight NEW TestRunner actions and all eight
JUnit cases pass, with no failures, errors, skips or retries.

| Arm | Native invocation                      | Gateway action | Session action | Summed TestRunner CPU |
| --- | -------------------------------------- | -------------: | -------------: | --------------------: |
| A1  | `9f5ba4c3-1431-4578-9389-741cde894218` |        98.258s |       196.140s |              585.168s |
| B1  | `83eb8dd8-b65d-45df-a61e-147abbd67902` |        80.254s |       116.434s |              292.237s |
| B2  | `43d832f9-78e4-419a-aa0e-d5982a2a6f8a` |        76.735s |       123.035s |              337.997s |
| A2  | `0f3264ef-3872-4d71-b3d1-54796df55cf4` |        86.692s |       158.872s |              397.309s |

Mean Gateway action wall is 92.475s A versus 78.494s B; Session is 177.506s versus
119.734s. Both candidate actions are shorter than both controls for each target.
Mean per-arm summed TestRunner CPU is 491.238s versus 315.117s. These are observed
two-arm means, not population estimates or a whole-CI saving.

Worker-local warming remains a confound: A1 Session records 1,354,547,213 download
bytes, B1 40,036 bytes, and B2/A2 omit that field. Reported worker metadata for each
target stays the same across arms. Normal action-cache counts also differ. The
same BUILD/data closure is retained; do not attribute those byte differences to
the mount change. Timestamp-based input fetch is separate from the ambiguous
`fileDownloadDurationUsec` field, which spans much of action wall here.

Every guest restores state ownership to host UID/GID 1000, removes its named
container and releases its listener. Forced Docker stop exits 143 as expected;
these Runtime contracts do not assert orderly guest powerdown. The boot-only
contract retains that separate assertion. Full allowlisted metrics and cleanup
receipts are in [the curated evidence](2026_11_13_guest_mounts.json).

The fixture optimization stays on the architecture branch: these manual targets
do not exist on devel and provide no current required-gate saving. A separate
coverage change shares the existing deployed VM shell probe with the direct
Claude Session. Its UID, PID1, cgroup placement and systemd denial assertions must
pass together with retained command IDs, two completed turns and sourced History;
executor resource enforcement remains disabled. Existing deployed VM acceptance
continues to own issuer/controller/KubeVirt/storage/App and Codex boundaries.
