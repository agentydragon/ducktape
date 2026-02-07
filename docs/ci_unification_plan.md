# CI Unification Plan

This document outlines a roadmap to consolidate CI tooling around Bazel while maintaining pre-commit for fast, change-aware validation.

## Current State

### Bazel-Managed (Hermetic)

- Python: ruff, mypy, pytest
- JS/TS: eslint, prettier, svelte-check, bundling
- Rust: clippy, rustfmt
- Formatting: shfmt, buildifier

### External Dependencies

| Tool                | Used By               | Purpose                               | Installation                                  |
| ------------------- | --------------------- | ------------------------------------- | --------------------------------------------- |
| opentofu            | pre-commit            | `terraform_fmt`, `terraform_validate` | Binary in PATH                                |
| tflint              | pre-commit            | `terraform_tflint`                    | Binary in PATH                                |
| fluxcd              | pre-commit            | `flux-build-dry-run`                  | Binary in PATH                                |
| kustomize           | pre-commit            | `kustomize-dry-run`                   | Binary in PATH                                |
| kubeconform         | pre-commit            | Kubernetes manifest validation        | Binary in PATH                                |
| checkov             | pre-commit            | Security analysis                     | Official pre-commit hook (manages own Python) |
| ansible-core        | pre-commit            | Playbook syntax check                 | pip install                                   |
| ansible-lint        | ansible-lint-full job | Full ansible validation               | pip install                                   |
| libdbus-1-dev, etc. | bazel-build           | Native Python C extensions            | apt install                                   |
| gitstatusd          | bazel-build           | wt package tests                      | http_archive in Bazel                         |
| PostgreSQL          | bazel-build           | Database tests                        | GitHub Actions service container              |

## Goals

### Target Environments

The same pre-commit hooks should work across three environments:

| Environment            | Tool Installation         | Notes                                                                              |
| ---------------------- | ------------------------- | ---------------------------------------------------------------------------------- |
| **Local dev**          | `.envrc` with nix/direnv  | Developer's machine, fast iteration                                                |
| **GitHub Actions CI**  | Official setup-\* actions | `opentofu/setup-opentofu`, `terraform-linters/setup-tflint`, `fluxcd/flux2/action` |
| **Claude Code on web** | apt + session start hooks | No Docker available, apt is fine                                                   |

### Requirements

1. **Pre-commit detects errors, autoformats, lints** - Same checks in all environments
2. **No Docker dependency** - Claude Code on web can't easily run Docker
3. **Graceful tool discovery** - Hooks work if tools are in PATH (however installed)
4. **Official pre-commit hooks where available** - e.g., `bridgecrewio/checkov` manages its own Python

### Tool Installation by Environment

| Tool      | Local (.envrc)                          | CI (GitHub Actions)                 | Claude Code (apt/session hook)     |
| --------- | --------------------------------------- | ----------------------------------- | ---------------------------------- |
| opentofu  | `nix profile install nixpkgs#opentofu`  | `opentofu/setup-opentofu@v1`        | `apt install tofu` or session hook |
| tflint    | `nix profile install nixpkgs#tflint`    | `terraform-linters/setup-tflint@v6` | Download binary in session hook    |
| flux      | `nix profile install nixpkgs#fluxcd`    | `fluxcd/flux2/action@main`          | Download binary in session hook    |
| checkov   | Official pre-commit hook                | Official pre-commit hook            | Official pre-commit hook           |
| kustomize | `nix profile install nixpkgs#kustomize` | apt or download                     | apt or session hook                |

### Design Principles

1. **Formatters/linters stay in pre-commit** - Aspects only see files with Bazel targets; formatters should catch all files
2. **Use official pre-commit hooks** - When available (checkov, ruff, prettier), prefer upstream hooks that manage their own environments
3. **CI uses GitHub Actions for tools** - Faster than nix, well-maintained, good caching
4. **Bazel for hermetic builds/tests** - Build artifacts, run tests, but not for universal formatting

### Session Start Hook (Claude Code on Web)

The session start hook (`claude_web_hooks/session_start.py`) configures the Claude Code on web environment.

**Current capabilities:**

- Sets up Bazel proxy configuration (for BCR access through TLS-inspecting proxy, see `claude_web_hooks/proxy-alternatives.md` for design rationale)
- Installs Bazelisk via binary download
- Installs pre-commit via pip (`pip install --user pre-commit==4.0.1`)
- Runs `pre-commit install` in the repo

**Missing for cluster pre-commit hooks:**

| Tool      | Installation Method            | Notes                                               |
| --------- | ------------------------------ | --------------------------------------------------- |
| opentofu  | Binary download from GitHub    | `tofu` binary for terraform_fmt, terraform_validate |
| tflint    | Binary download from GitHub    | For terraform_tflint hook                           |
| flux      | Binary download from GitHub    | For flux-build-dry-run hook                         |
| kustomize | Binary download from GitHub    | For kustomize-dry-run hook                          |
| kubeseal  | Binary download from GitHub    | For validate-sealed-secrets hook                    |
| helm      | apt install or binary download | For helm-template-dry-run hook                      |

**Implementation approach:**

The existing `nix_setup.py` has infrastructure for tool installation. Add new functions to install tools via binary download (similar to `install_bazelisk()`):

```python
def install_opentofu(version: str = "1.11.2") -> bool:
    """Download and install OpenTofu binary."""
    url = f"https://github.com/opentofu/opentofu/releases/download/v{version}/tofu_{version}_linux_amd64.zip"
    # Download, extract, install to ~/.local/bin
    ...

def install_tflint(version: str = "0.53.0") -> bool:
    """Download and install tflint binary."""
    url = f"https://github.com/terraform-linters/tflint/releases/download/v{version}/tflint_linux_amd64.zip"
    ...

def install_flux(version: str = "2.4.0") -> bool:
    """Download and install flux CLI binary."""
    url = f"https://github.com/fluxcd/flux2/releases/download/v{version}/flux_{version}_linux_amd64.tar.gz"
    ...
```

**Conditional installation:**

Only install cluster tools if `cluster/` directory exists (hooks are scoped to `^cluster/` paths anyway).

## Progress (Updated 2026-01-08)

### Completed

- **rules_tf integration** - Added to MODULE.bazel (v0.0.10) with OpenTofu 1.11.2, tflint 0.53.0
- **tfmirror.dev validation** - Tested and working! Terraform validate works via network mirror
- **tofu_validate.sh wrapper** - Created `cluster/scripts/tofu_validate.sh` using tfmirror.dev
- **Terraform BUILD.bazel** - Added `cluster/terraform/00-persistent-auth/BUILD.bazel` with:
  - `tf_module` for tflint (hermetic)
  - `sh_test :validate` for terraform validate (with tfmirror.dev)
- **rules_kustomize** - Added to MODULE.bazel (v0.5.2)
- **http_archive binaries** - Added to MODULE.bazel:
  - `@kustomize//:kustomize` (v5.5.0)
  - `@kubeconform//:kubeconform` (v0.6.7)
  - `@flux//:flux` (v2.4.0)
  - `@gitstatusd//:gitstatusd-linux-x86_64` (v1.5.4)

### In Progress

- Create k8s validation BUILD.bazel using the http_archive binaries

### Completed (This Session)

- **CI workflow simplified** - Replaced `setup-nix-direnv` with `bazelbuild/setup-bazelisk@v3` for
  `props-frontend-build`, `visual-regression`, and `bazel-build` jobs.
- **CI uses official GitHub Actions** - Pre-commit job now uses:
  - `opentofu/setup-opentofu@v1` (replaces nix)
  - `terraform-linters/setup-tflint@v4` (replaces nix)
  - `fluxcd/flux2/action@main` (replaces nix)
  - Binary downloads for kubeconform, kustomize, kubeseal, helm
- **props-frontend-build removed** - Folded into bazel-build job
- **Visual regression hermetic** - Uses `rules_playwright` for Chromium browser
- **Session start hook extended** - Added kubeseal and helm to cluster tools (opentofu, tflint, flux, kustomize, kubeseal, helm)

### Decisions Made

- **Checkov stays in pre-commit** - Now uses official `bridgecrewio/checkov` pre-commit hook
  (manages own Python environment). Removed nix-shell wrapper.
- **Prettier stays in pre-commit** - The Bazel-built prettier runs via local hook. Aspects can't
  catch files without Bazel targets, and formatters should apply universally.
- **Ruff stays in pre-commit** - Same reasoning as prettier - should catch any Python file,
  not just those with associated Bazel targets.

### Remaining

- Evaluate ansible-lint in Bazel (galaxy dependency challenge)

## Phase 1: Immediate Fixes

### 1.1 Switch to `bazelbuild/setup-bazelisk`

Replace the failing Nix-based bazelisk installation:

```yaml
# Before (setup-nix-direnv)
- uses: DeterminateSystems/nix-installer-action@v17
- run: nix profile install nixpkgs#bazelisk
- run: ln -sf "$HOME/.nix-profile/bin/bazelisk" "$HOME/.nix-profile/bin/bazel" # FAILS

# After
- uses: bazelbuild/setup-bazelisk@v3
```

### 1.2 Keep Nix Only for Cluster Tools (pre-commit job)

```yaml
- name: Install cluster validation tools
  run: |
    nix profile install nixpkgs#opentofu nixpkgs#tflint nixpkgs#fluxcd
    echo "$HOME/.nix-profile/bin" >> $GITHUB_PATH
```

### 1.3 Fetch gitstatusd via http_archive

Add to `MODULE.bazel`:

```starlark
http_archive(
    name = "gitstatusd",
    urls = ["https://github.com/romkatv/gitstatus/releases/download/v1.5.4/gitstatusd-linux-x86_64.tar.gz"],
    sha256 = "...",  # TODO: compute
    build_file_content = """
exports_files(["gitstatusd-linux-x86_64"])
""",
)
```

Then in wt tests, use `$(location @gitstatusd//:gitstatusd-linux-x86_64)`.

**Status**: Ready to implement. Release URL confirmed: `https://github.com/romkatv/gitstatus/releases/download/v1.5.4/gitstatusd-linux-x86_64.tar.gz`

## Phase 2: Optimize Pre-commit in CI

### 2.1 Run Only on Changed Files

Use `--from-ref` and `--to-ref` to validate only changed files:

```yaml
- name: Run pre-commit on changed files
  run: |
    if [ "${{ github.event_name }}" = "pull_request" ]; then
      git fetch origin ${{ github.base_ref }}
      pre-commit run --from-ref origin/${{ github.base_ref }} --to-ref HEAD
    else
      # Push to main: check last commit only
      pre-commit run --from-ref HEAD~1 --to-ref HEAD
    fi
```

**Benefits**:

- Faster CI for small changes
- Cluster hooks only run when cluster/ files change
- Still catches all issues (hooks are file-scoped)

**Reference**: [pre-commit documentation](https://pre-commit.com/) and [pre-commit/action](https://github.com/pre-commit/action)

### 2.2 Alternative: Use pre-commit/action with extra_args

```yaml
- uses: pre-commit/action@v3.0.1
  with:
    extra_args: --from-ref origin/${{ github.base_ref }} --to-ref HEAD
```

## Phase 3: Bazelify Terraform Validation

### Evaluation Results (Prototyped)

Tested rules_tf (v0.0.10) with OpenTofu 1.11.2 on `cluster/terraform/00-persistent-auth`:

**What works:**

- tflint runs and found 12 real issues (missing version constraints, outputs in wrong files, unused providers)
- `bazel build` succeeds and creates module tarballs
- OpenTofu/tflint binaries download correctly

**What doesn't work out of the box:**

- `terraform validate` requires pre-mirrored providers
- rules_tf's `mirror` parameter is for declaring providers to download locally, not network mirror URLs
- Error: "provider registry.opentofu.org/hashicorp/local was not found in any of the search locations"

### Option A: Use tfmirror.dev Network Mirror (TESTED - WORKS!)

[tfmirror.dev](https://tfmirror.dev/) is a free public network mirror for Terraform/OpenTofu providers.

**Tested successfully on 2026-01-08:**

```bash
# Create .tofurc with tfmirror.dev
cat > /tmp/tofurc <<'EOF'
provider_installation {
  network_mirror {
    url = "https://tfmirror.dev/"
  }
}
EOF
export TF_CLI_CONFIG_FILE=/tmp/tofurc

# Run validation
cd cluster/terraform/00-persistent-auth
tofu init -backend=false -input=false
tofu validate
# Result: Success! The configuration is valid.
```

**Implementation approach:**

1. Create `cluster/scripts/tofu_validate.sh` wrapper (already created)
2. Use sh_test with `tags = ["requires-network"]` to allow tfmirror.dev access
3. The tofu binary comes from rules_tf toolchain

```starlark
# cluster/terraform/00-persistent-auth/BUILD.bazel
sh_test(
    name = "validate",
    srcs = ["//cluster/scripts:tofu_validate.sh"],
    data = glob(["**/*.tf"]) + ["@tf_toolchains//:tofu"],
    args = ["$(location .)"],
    tags = ["requires-network"],  # Allow tfmirror.dev access
)
```

### Option B: rules_kustomize for K8s + sh_test for Terraform

[rules_kustomize](https://registry.bazel.build/modules/rules_kustomize) v0.5.2 is available on BCR:

```starlark
bazel_dep(name = "rules_kustomize", version = "0.5.2")
```

For terraform, use sh_test wrappers with network access:

```starlark
# cluster/terraform/BUILD.bazel
sh_test(
    name = "tflint",
    srcs = ["//tools:run_tflint.sh"],
    data = glob(["**/*.tf"]),
    tags = ["requires-network"],  # Allow network for provider download
)
```

### Option C: Pre-mirror Providers (Hermetic but Complex)

1. Run `tofu providers mirror` to download all providers locally
2. Store in GCS/S3 or commit to repo (large!)
3. Configure rules_tf `mirror` parameter with local paths

**Trade-off:** Full hermeticity but significant setup and storage overhead.

### Recommendation

1. **Use rules_tf for tflint** - Works hermetically, finds real issues
2. **Add sh_test with tfmirror.dev for validate** - Network access but cached results
3. **Keep `terraform_fmt` in pre-commit** - Trivial, fast, no deps

**What rules_tf replaces:**

- `terraform_tflint` hook -> `tf_module :lint` target

**What sh_test with tfmirror.dev adds:**

- `terraform_validate` hook -> `sh_test :validate` target (with network access)

**What stays in pre-commit:**

- `terraform_fmt` (trivial, fast)

## Phase 4: Bazelify Kubernetes Validation

### Current Hooks

| Hook               | Tool        | What it does                            |
| ------------------ | ----------- | --------------------------------------- |
| kubeconform        | kubeconform | Schema validation against K8s API specs |
| k8svalidate        | k8svalidate | Additional K8s manifest checks          |
| kustomize-dry-run  | kustomize   | Build all kustomizations                |
| flux-build-dry-run | flux CLI    | Validate Flux can render manifests      |

### Available Bazel Rules

**[rules_kustomize](https://registry.bazel.build/modules/rules_kustomize)** (v0.5.2 on BCR):

- Provides `kustomize_build` rule that invokes `kustomize build`
- Output is a YAML file that can be consumed by other rules
- Supports golden test targets for validation

```starlark
bazel_dep(name = "rules_kustomize", version = "0.5.2")
```

**[rules_gitops](https://github.com/adobe/rules_gitops)** (Adobe):

- More deployment-focused (`k8s_deploy`, `.apply`, `.gitops` targets)
- Includes kustomize integration
- Better suited if you want Bazel to manage deployments

**No dedicated rules for:**

- kubeconform - Need sh_test wrapper with http_archive
- flux CLI - Need sh_test wrapper with http_archive

### Recommended Approach

#### 4.1 Use rules_kustomize for kustomize validation

```starlark
load("@rules_kustomize//:defs.bzl", "kustomization")

# Validate each kustomization builds successfully
kustomization(
    name = "apps",
    srcs = glob(["apps/**/*.yaml"]),
    kustomization = "apps/kustomization.yaml",
)
```

#### 4.2 Add kubeconform via http_archive

```starlark
# MODULE.bazel
http_archive(
    name = "kubeconform",
    urls = ["https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz"],
    sha256 = "...",
    build_file_content = 'exports_files(["kubeconform"])',
)
```

```starlark
# k8s/BUILD.bazel
sh_test(
    name = "kubeconform_test",
    srcs = ["//tools:kubeconform_test.sh"],
    data = [
        "@kubeconform//:kubeconform",
    ] + glob(["**/*.yaml"], exclude = ["**/flux-system/**"]),
)
```

#### 4.3 Add flux via http_archive

```starlark
# MODULE.bazel
http_archive(
    name = "flux",
    urls = ["https://github.com/fluxcd/flux2/releases/download/v2.4.0/flux_2.4.0_linux_amd64.tar.gz"],
    sha256 = "...",
    build_file_content = 'exports_files(["flux"])',
)
```

#### 4.4 Existing Python validation scripts

The existing scripts (`validate-kustomizations.py`, `validate-flux-build.py`) can be wrapped as py_test:

```starlark
py_test(
    name = "validate_kustomizations",
    srcs = ["scripts/validate-kustomizations.py"],
    data = glob(["k8s/**/*.yaml"]),
    deps = ["@pypi//pyyaml"],
    main = "scripts/validate-kustomizations.py",
    args = ["--root", "k8s/"],
)
```

### Trade-off Analysis

| Approach               | Hermetic | Caching | Complexity |
| ---------------------- | -------- | ------- | ---------- |
| rules_kustomize        | Yes      | Yes     | Low        |
| sh_test + http_archive | Partial  | Yes     | Medium     |
| Keep in pre-commit     | No       | No      | Low        |

**Recommendation:** Use rules_kustomize for kustomize validation, sh_test wrappers for kubeconform/flux

## Phase 5: Bazelify Ansible-lint

### Approach

Add ansible-lint as a Python dependency and create a test rule:

```starlark
# requirements_bazel.txt
ansible-lint>=24.0.0
ansible-core>=2.18

# ansible/BUILD.bazel
py_test(
    name = "ansible_lint_test",
    srcs = ["//tools:ansible_lint_runner.py"],
    data = glob(["**/*.yaml", "**/*.yml"]),
    deps = ["@pypi//ansible_lint"],
    args = ["--config-file", "$(location .ansible-lint)"],
)
```

**Benefits**:

- Removes ansible-lint-full job entirely
- Runs as part of `bazel test //...`
- Hermetic, cached

**Open Questions**:

1. Does ansible-lint work well in Bazel sandbox? (May need network for Galaxy)
2. How to handle ansible-galaxy dependencies?

## Recommended End State

### CI Jobs (Simplified)

```yaml
jobs:
  pre-commit:
    # Full validation with --all-files
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install pre-commit ansible-core
      - run: pre-commit run --all-files

  bazel:
    # Comprehensive build, lint, test
    # Everything else is Bazel-managed
    runs-on: ubuntu-latest
    services:
      postgres: ...
    steps:
      - uses: actions/checkout@v4
      - uses: bazelbuild/setup-bazelisk@v3
      - run: sudo apt-get install -y libdbus-1-dev libgirepository-2.0-dev libcairo2-dev
      - run: |
          bazel build //...
          bazel build --config=check //...
          bazel test //...
```

### What Stays in Pre-commit

- Conflict marker check (trivial, no deps)
- YAML/TOML syntax (trivial)
- `terraform_fmt` (trivial, fast)
- `terraform_validate` (needs network for providers)
- Ansible playbook syntax (fast)

### What Moves to Bazel

- All Python linting (already done)
- All JS/TS linting (already done)
- Rust linting (already done)
- gitstatusd (http_archive)
- tflint (Phase 3, rules_tf)
- Kubernetes validation - kustomize (Phase 4, rules_kustomize)
- Kubernetes validation - kubeconform/flux (Phase 4, http_archive + sh_test)
- Ansible-lint (Phase 5)

## Open Questions for Discussion

1. ~~**rules_tf provider validation**: Can we use tfmirror.dev as network mirror?~~ **RESOLVED**: Yes! Created `tofu_validate.sh` wrapper that sets `TF_CLI_CONFIG_FILE` to use tfmirror.dev. Test passes: `bazel test //cluster/terraform/00-persistent-auth:validate`

2. **ansible-galaxy**: The ansible-lint job needs galaxy dependencies. How should Bazel handle this? (Network access in test? Pre-fetch? Skip?)

## Next Steps

1. ~~**Immediate**: Fix CI with `bazelbuild/setup-bazelisk`~~ (DONE - props-frontend-build, visual-regression, bazel-build jobs)
2. ~~**Short-term**: Add gitstatusd via http_archive~~ (DONE - added to MODULE.bazel)
3. ~~**Phase 3**: Keep rules_tf for tflint, explore tfmirror.dev for terraform validate~~ (DONE - working!)
4. ~~**Phase 4**: Add rules_kustomize + http_archive for kubeconform/flux~~ (DONE - binaries added)
5. ~~**CI pre-commit job**: Replace nix with official GitHub Actions for opentofu, tflint, flux~~ (DONE - using official actions + binary downloads)
6. ~~**Session start hook**: Add cluster tool installation (opentofu, tflint, flux, kustomize, kubeseal, helm) via binary downloads~~ (DONE)
7. **Phase 4 continued**: Create k8s validation BUILD.bazel with sh_test wrappers
8. **Phase 5**: Evaluate ansible-lint in Bazel (galaxy dependency challenge)

## References

- [rules_tf](https://github.com/yanndegat/rules_tf) - Bazel rules for Terraform/OpenTofu
- [rules_k8s](https://github.com/bazelbuild/rules_k8s) - Bazel rules for Kubernetes
- [pre-commit documentation](https://pre-commit.com/) - `--from-ref` and `--to-ref` usage
- [pre-commit/action](https://github.com/pre-commit/action) - GitHub Action for pre-commit
- [gitstatusd releases](https://github.com/romkatv/gitstatus/releases) - Binary downloads
