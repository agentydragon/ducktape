# Checkov in GHA Pre-commit: Options and Cost Analysis

Investigated 2026-03-30. Checkov is a Terraform security scanner running as a
pre-commit hook, scoped to `cluster/terraform/*.tf` files.

## Current Setup

`.pre-commit-config.yaml` configures checkov as a **repo-hosted hook** (not `language: system`):

```yaml
- repo: https://github.com/bridgecrewio/checkov
  rev: 3.2.513
  hooks:
    - id: checkov_diff
      args: ["--framework", "terraform", "--skip-check", "CKV_TF_1", "-f"]
      files: "^cluster/terraform/.*\\.tf$"
```

Pre-commit downloads and pip-installs checkov (~200 transitive deps) into its own
virtualenv. This is unlike most other hooks which use `language: system` (provided
by the nix `web-session` closure).

## GHA Failure Modes

The `pre-commit.yml` workflow runs `pre-commit run --all-files`. Checkov failures:

1. **Pip install timeout/network errors** — 200 transitive deps, large downloads (botocore alone is 107 MiB)
2. **Cache misses** — any rev bump in `.pre-commit-config.yaml` rebuilds the entire virtualenv
3. **Native extension build failures** — numpy, rustworkx, igraph require compilation if no wheel is available

The pre-commit cache (`~/.cache/pre-commit`) is saved/restored in the workflow
(lines 55-62, 79-84), so warm runs are fast. Cold runs are the problem.

## Fix Options

### Option 1: Improve pre-commit caching (low effort, no closure cost)

Keep current setup. Ensure cache key stability by not bumping checkov rev
unnecessarily. The workflow already caches `~/.cache/pre-commit`. Cold installs
take ~13s but are reliable when the network is stable.

**Verdict**: Good enough if failures are rare.

### Option 2: Switch to `language: system` via nix (medium effort, large closure cost)

Add `pkgs.checkov` to the `web-session` `symlinkJoin` in `flake.nix`, then change
the hook to `language: system`. Eliminates pip install entirely.

**Verdict**: +508 MiB closure cost is hard to justify for one hook.

### Option 3: Separate GHA step (medium effort, no closure cost)

Move checkov out of pre-commit into a dedicated workflow step with `pip install checkov`
and its own caching. More control over install and caching.

**Verdict**: Adds workflow complexity but isolates checkov failures from other hooks.

## Nix Closure Cost: Exact Measurements

Measured using the flake's own nixpkgs revision (`checkov-3.2.495`).

| Metric                        | Value         |
| ----------------------------- | ------------- |
| Current `web-session` closure | 1805.7 MiB    |
| `checkov` standalone closure  | 866.0 MiB     |
| Shared store paths            | 100 of 199    |
| New store paths from checkov  | 99            |
| **Incremental size**          | **508.1 MiB** |
| **Percentage increase**       | **28.1%**     |

### Top new dependencies by size

|    Size | Package                                    |
| ------: | ------------------------------------------ |
| 107 MiB | `botocore` (AWS SDK core)                  |
|  63 MiB | `lapack-3` (linear algebra, via numpy)     |
|  63 MiB | `blas-3` (linear algebra, via numpy)       |
|  53 MiB | `numpy`                                    |
|  31 MiB | `openblas`                                 |
|  23 MiB | `networkx` (graph algorithms)              |
|  22 MiB | `checkov` itself                           |
|  19 MiB | `systemd-minimal`                          |
|  13 MiB | `gfortran-lib` (Fortran runtime, via blas) |
|  13 MiB | `setuptools`                               |

The scientific computing stack (numpy/blas/lapack/openblas/gfortran = ~223 MiB)
and AWS SDK (botocore = ~107 MiB) dominate. These are transitive deps for checkov's
policy evaluation and cloud scanning — no easy way to trim them.

### Why so large

Checkov bundles policy engines for AWS/Azure/GCP/K8s. Even though we only use
`--framework terraform`, the nix package includes the full dependency tree.
A hypothetical `checkov-slim` nix package (terraform-only) doesn't exist in nixpkgs.
