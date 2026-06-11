# GitHub Copilot Instructions

For detailed repository guidance, see: [AGENTS.md](../AGENTS.md) and [STYLE.md](../STYLE.md)

## Repository Overview

"Ducktape" is a personal infrastructure repository. Key areas:

- **Agent Framework** (`agent_cli/`, `x/agent_server/`, `agent_pkg/`) - Agent REPL, FastAPI backend, runtime
- **Props** (`props/`) - Code evaluation system with Docker-based E2E tests
- **MCP Infrastructure** (`mcp_infra/`) - MCP compositor and utilities
- **Infrastructure Automation** (`ansible/`) - System configuration and deployment
- **Cluster** (`cluster/`) - k8s cluster configuration
- **Dotfiles** (`nix/home/`) - Nix home-manager configs

## Build System

**Bazel** is the unified build system. Always use Bazel, never direct `pytest` or `python`.

### Remote Builds (`bb remote`)

Prefer `bb remote` over direct `bazel` for build, test, and query commands. `bb remote`
runs Bazel on a BuildBuddy runner VM colocated with RBE/cache servers, giving fast builds
with warm Bazel instances. It automatically syncs local git diffs to the remote runner.

```bash
# Prefer:
bb remote build //path/to:target
bb remote test //path/to:target
bb remote query '...'

# Use direct bazel only for:
#   - bazel run (local side effects)
#   - When you need build outputs on the local filesystem
#   - Gazelle: bazel run //devinfra:gazelle
```

### Gazelle

```bash
bazel run //devinfra:gazelle         # Update BUILD files
```

Python 3.13+. Dependencies in `requirements_bazel.txt`.

### Terraform via Bazel

Terraform/OpenTofu modules are managed by Bazel (`@rules_tf`). Each module has a
`BUILD.bazel` with `tf_providers_versions` and `tf_module` rules. **There are no
hand-written `terraform.tf` or `required_providers` blocks** — Bazel generates them
from `tf_providers_versions`. Do not suggest adding `terraform { required_providers }`
blocks manually.

### Flux Kustomization Wiring

Flux `Kustomization` resources (`flux-kustomization.yaml`) are applied from the **root**
`cluster/k8s/kustomization.yaml`, not from local `kustomization.yaml` files in each
directory. A directory's `kustomization.yaml` should only list the manifests that Flux
applies at `spec.path` (e.g., `terraform.yaml`, `*.sops.yaml`). **Do not include
`flux-kustomization.yaml` in local `kustomization.yaml` resources** — it causes
redundant application.

### Container Images

Container images are built with Bazel (`rules_oci`, `rules_distroless`), not Dockerfiles.
The `.github/workflows/push-images.yml` matrix builds each `oci_image` on RBE,
downloads the layout to the GHA runner, and pushes via `crane`. Pushes are
content-deduped against the newest `devel-*` tag in the registry so unchanged images
don't trigger Flux deployment rolls. The RBE worker image and a few upstream-derived
images (OpenClaw, Tana MCP) still use Dockerfiles with GitHub Actions workflows, but
all new images should use `oci_image` + a row in `push-images.yml`'s matrix.

## Verification (Required)

Before handing in any work:

```bash
bb remote build //... --config=rbe
bb remote test //... --config=rbe
```

If you modified `ansible/`, follow the checklist in [ansible/AGENTS.md](../ansible/AGENTS.md).

## Testing

- Tests: `test_*.py` adjacent to the code they test
- Framework: pytest with pytest-asyncio (auto mode)
- All `py_test` targets MUST have `pytest_bazel.main()` entry point
- Do NOT add `@pytest.mark.asyncio` — auto mode handles it
