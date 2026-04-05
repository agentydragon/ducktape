# GitHub Copilot Instructions

For detailed repository guidance, see: [AGENTS.md](../AGENTS.md) and [STYLE.md](../STYLE.md)

## Repository Overview

"Ducktape" is a personal infrastructure repository. Key areas:

- **Agent Framework** (`agent_cli/`, `agent_server/`, `agent_core/`, `agent_pkg/`) - Agent REPL, FastAPI backend, runtime
- **Props** (`props/`) - Code evaluation system with Docker-based E2E tests
- **MCP Infrastructure** (`mcp_infra/`) - MCP compositor and utilities
- **Infrastructure Automation** (`ansible/`) - System configuration and deployment
- **Development Tools** (`wt/`) - Worktree management
- **Dotfiles** (`dotfiles/`, `nix/home/`) - Shell configs (mostly Nix home-manager now)
- **Cluster** (`cluster/`) - k8s cluster configuration

## Build System

**Bazel** is the unified build system. Always use Bazel, never direct `pytest` or `python`:

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
Images are pushed to GHCR via `ghcr_push` targets, triggered by BuildBuddy CI. The RBE
worker image and a few upstream-derived images (OpenClaw, Tana MCP) still use Dockerfiles
with GitHub Actions workflows, but all new images should use `oci_image` + `ghcr_push`.

## Verification (Required)

Before handing in any work, run `bazel build //...` and `bazel test //...`.

If you modified `ansible/`, follow the checklist in [ansible/AGENTS.md](../ansible/AGENTS.md).

## Testing

- Tests: `test_*.py` adjacent to the code they test
- Framework: pytest with pytest-asyncio (auto mode)
- All `py_test` targets MUST have `pytest_bazel.main()` entry point
- Do NOT add `@pytest.mark.asyncio` — auto mode handles it

## Props E2E Tests

Props E2E tests use per-test testcontainers (PostgreSQL, Docker registry, etc.) and are fully hermetic — no manual setup required. Just run:

```bash
bazel test //props/...
```
