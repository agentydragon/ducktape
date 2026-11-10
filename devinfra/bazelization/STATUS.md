# Bazel migration record

## February 2026 snapshot

The migration note recorded 6 of 8 success criteria met: Python coverage 95.6%
(990/1036 files), Rust 100%, Terraform 100% (76/76 modules), and 12 Docker images
moved to `rules_oci`. These are historical counts, not current coverage claims.
The intended shape was colocated tests, one dependency source per language,
Bazel-managed build/test/image work, and a single root Python tool configuration.

## Scope and environment boundaries

- Ansible/Galaxy, Nix configuration, and system/deployment shell scripts remain
  separate concerns. Shell categories include CI/Ansible, container entrypoints,
  deployment scripts, Nix-managed programs, and Bazel test helpers.
- [finance/gnucash_util.py](../../finance/gnucash_util.py) imports the system
  `gnucash` library. [GNOME theme targets](../../gnome/gterm_theme/BUILD.bazel) are
  manual and need the GNOME/DBus environment and system libraries.
- The website's build/deployment owner is [website/README.md](../../website/README.md).

## Unimplemented intentions from the note

A unified `bazel check` command and consolidation of small experimental packages
were proposals, not implemented commands or a completed migration. Separate
packages still matter when deployment targets or dependencies differ.

## Current owners

Use the [root README](../../README.md#build-system) and [AGENTS.md](../../AGENTS.md)
for build/test policy, [linting architecture](../docs/linting.md) for aspects and
hook ownership, [Gazelle](../docs/gazelle.md) for BUILD generation, and
[lockfiles](../docs/lockfiles.md) for dependency updates. The existing
[`find_orphans_bin`](../orphans/BUILD.bazel) target reports files outside the graph.
