# VM test guest kernel update after a boot OOPS

The k3s VM fixture pinned `linux-image-6.12.63+deb13-cloud-amd64` version `6.12.63-1` from
the Debian 2026-03-01 snapshot in `MODULE.bazel`. A separate guest in BuildBuddy invocation
[`8241597f`](https://app.buildbuddy.io/invocation/8241597f-5385-59ba-af39-0e3c6b901b5e)
logged a boot-time kernel OOPS while running the PR #14005 VM suite:

- `ext4lazyinit` dereferenced address `0x157`;
- the faulting instruction was `submit_bio_noacct+0x134`;
- the guest reported Debian `6.12.63+deb13-cloud-amd64 #1`, package `6.12.63-1`.

The OOPS occurred in VM 2, separately from VM 1, where the new systemd invocation-fence
subtest passed. The test run later timed out and found a leftover Sandbox and `/dev/loop1` during
teardown; this evidence does not establish that the OOPS caused that timeout or cleanup state.

This signature is a strong match for the upstream blk-throttle activation race described by
[CVE-2025-40147](https://kernel.googlesource.com/pub/scm/linux/security/vulns/+/d0806ccc20dbf7f56d6937048a3f4dbf55f09bdb/cve/published/2025/CVE-2025-40147.mbox): that report describes a NULL dereference from `blk_should_throtl()` inside `submit_bio_noacct()` and identifies fix `7b4bfd02c934dbbb18814ed012ecb90b58db6935`, included upstream in 6.12.78. Debian's [security tracker](https://security-tracker.debian.org/tracker/CVE-2025-40147) marks trixie fixed at source package `linux` `6.12.85-1`. The available x86 console output does not name `blk_should_throtl()` or otherwise prove that this was the same defect, so CVE identity remains unconfirmed.

The test-only pin now uses Debian stable-security's signed cloud image `6.12.115-2`, which is newer
than Debian's fixed baseline. The immutable snapshot package first appeared at
`20261102T161514Z`; its SHA-256 is
`c26573a3b5dca6bd3bf2b06a13b81cbb845c04eb6e35d7ad57f4b885746153e4`. This change only affects
the K3s system-test guest; it does not change the production NixOS kernel.

Validation: pending.
