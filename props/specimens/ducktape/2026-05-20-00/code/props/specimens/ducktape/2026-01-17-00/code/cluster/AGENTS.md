@README.md

# Agent Instructions

## ⚠️ CRITICAL: REPOSITORY SCOPE

**Your work is confined to this cluster repository (`/home/agentydragon/code/cluster`) ONLY.**

**FORBIDDEN OPERATIONS:**

- Editing or pushing files outside this repository (e.g., ~/code/ducktape, other repos)
- Making commits in other repositories without explicit instruction
- Pushing changes to other git repositories (user controls those separately)

**PERMITTED OPERATIONS:**

- Reading reference code from `/code/*` for documentation and implementation patterns
- Suggesting changes for other repositories (user will implement them)
- Working with files exclusively in `/home/agentydragon/code/cluster`

**EXCEPTION:** Only edit/commit/push to other repositories when user explicitly instructs you to do so.

## ⚠️ CRITICAL: BOOTSTRAP TERMINOLOGY

**When the user says "bootstrap the cluster", "tear down the cluster", "recreate the cluster", or similar phrases:**

### DEFAULT SCOPE: Layer 01 (Infrastructure) and above

- `terraform destroy` in `terraform/01-infrastructure/` (VMs)
- `./bootstrap.sh` (recreates VMs, installs Talos, deploys services)
- Layers affected: 01-infrastructure, 02-services, 03-configuration

### EXCLUDED BY DEFAULT: Layer 00 (Persistent Auth)

- `terraform/00-persistent-auth/` is NOT destroyed unless explicitly stated
- Includes: Sealed secrets keypair, CSI tokens, Nix signing keys, JWT tokens
- These persist in terraform state across cluster lifecycles
- Only destroy when user explicitly says "including persistent auth" or "from scratch"

### WHY: The persistent auth layer is designed to survive VM destroy/recreate cycles to avoid

- Sealed secrets re-encryption (entire repo would need commits)
- Proxmox token regeneration causing desynchronization
- Nix cache signing key regeneration breaking cache trust

**EXAMPLES:**

- ✅ "bootstrap the cluster" → Start from layer 01 (VMs)
- ✅ "tear down and recreate" → Destroy VMs, keep layer 00
- ✅ "full teardown including persistent auth" → Destroy everything including layer 00
- ✅ "bootstrap from scratch" → Destroy everything, regenerate all secrets

## TASK DELEGATION AND PARALLELIZATION

**Use the Task tool proactively to delegate complex subtasks to specialized agents.**

### When to Delegate to Agents

**ALWAYS delegate these to agents:**

- Complex diagnostics: "diagnose why X is broken"
- Multi-step investigations: "check status of Y and diagnose if broken"
- Research tasks: "find and analyze all occurrences of Z"
- Independent workstreams that can run in parallel
- Tasks requiring deep exploration of unfamiliar codebases

**Examples:**

- ❌ DON'T: Manually grep/search for nginx config issues
- ✅ DO: `Task: "diagnose VPS nginx SNI routing - wildcard pattern not matching"`

- ❌ DON'T: Manually check multiple Terraform resources sequentially
- ✅ DO: `Task: "investigate why SSO Terraform resources stuck in reconciliation"`

### Parallelization Strategy

**When you have multiple independent workstreams, spawn agents in PARALLEL using a single message with multiple Task calls.**

Example:

```text
User: "we have 3 issues: SNI routing broken, Terraform stuck, and need Matrix SSO"
Assistant: *spawns 3 Task agents in parallel in ONE message*
```

**Benefits:**

- Maximize throughput and efficiency
- Work on multiple problems simultaneously
- Reduce context window usage by offloading to specialized agents
- Each agent has full context and autonomy for its specific task

### Agent Task Specifications

When spawning agents, provide:

1. **Clear objective**: What needs to be accomplished
2. **Current state**: What we know so far
3. **Expected deliverable**: What information to report back
4. **Constraints**: Any restrictions or requirements
5. **Reference code location**: Inform agents about `/code`
6. **CLAUDE.md compliance**: Instruct agents to read and follow CLAUDE.md, especially PRIMARY DIRECTIVE

**IMPORTANT**: Always inform subagents about the reference code convention (see "Reference Code Location"
section below for details).

**Agent Context Template:**

When spawning agents, include this context in your prompt:

```text
IMPORTANT CONTEXT:
- Reference code available at /code using domain.tld/org/repo structure
- Read and follow /home/agentydragon/code/cluster/CLAUDE.md
- PRIMARY DIRECTIVE: All fixes must be declarative, committed configuration changes
- No manual kubectl patches as solutions - only for debugging to understand issues
- All work confined to /home/agentydragon/code/cluster repository only
```

## PRIMARY DIRECTIVE: DECLARATIVE TURNKEY BOOTSTRAP

**The primary goal is to achieve a committed repo state where the bootstrap script → everything works.**

## ⚠️ CRITICAL: PERSISTENT AUTH PROTECTION

**AI agents and automated processes MUST NEVER destroy the persistent auth layer (00-persistent-auth) without
explicit user authorization.**

**FORBIDDEN OPERATIONS:**

- `cd terraform/00-persistent-auth && terraform destroy`
- Any command that would destroy CSI tokens or sealed secrets keypair
- "Clean slate" operations that include persistent auth

**PERMITTED OPERATIONS:**

- VM lifecycle: `cd terraform/01-infrastructure && terraform destroy && terraform apply`
- Services reset: Layers 02-services, 03-configuration
- Selective bootstrap: `./bootstrap.sh --start-from=infrastructure`

**RATIONALE:** The persistent auth layer contains:

- Proxmox CSI tokens (required for storage)
- Sealed secrets keypair (required for secret decryption)
- These survive VM teardown by design to prevent git commit churn and maintain storage continuity

## ⚠️ CRITICAL: COMMIT BEFORE RECONCILE

**NEVER attempt to reconcile Flux resources (HelmRelease, Kustomization, etc.) until changes are committed AND
pushed to origin.**

**MANDATORY WORKFLOW:**

1. Make changes to chart/manifest files
2. `git add -A && git commit -m "..." && git push`
3. ONLY THEN: `flux reconcile source git ...` followed by `flux reconcile helmrelease ...`

**WHY THIS MATTERS:**

- Flux fetches configuration from the git repository, not your local filesystem
- Reconciling before push = Flux uses OLD configuration = changes don't apply
- This wastes time trying to debug "why isn't my change working" when it simply hasn't been pushed yet

**SYMPTOMS OF FORGETTING TO PUSH:**

- Pods still show old errors after "fixing" them
- Environment variables not updated in deployment
- Template changes not reflected in rendered manifests
- Repeated reconciliation attempts with no effect

**CORRECT SEQUENCE:**

```bash
# 1. Edit files
vim charts/powerdns/templates/deployment.yaml

# 2. Commit and push FIRST
git add -A
git commit -m "fix: add missing environment variable"
git push

# 3. ONLY NOW reconcile Flux
flux reconcile source git powerdns-chart -n dns-system
flux reconcile helmrelease powerdns -n dns-system
```

**NEVER DO THIS:**

```bash
# ❌ WRONG: Reconciling before push
vim charts/powerdns/templates/deployment.yaml
flux reconcile helmrelease powerdns -n dns-system  # This uses OLD code!
git add -A && git commit && git push  # Too late, already tried to deploy
```

### Objective

Achieve a committed repository state such that:

1. `./bootstrap.sh` (the ONLY supported bootstrap method)
2. **Everything works**

Where "everything" means everything currently in plan.md scope as specified by user.

### Scope Evolution Strategy

**Spiral development approach:**

- **v0**: Turnkey basic cluster
- **v1**: Add service X, iterate until reliable and turnkey, commit when working
- **v2**: Add service Y, iterate until reliable and turnkey, commit when working
- **v∞**: Eventually migrate services from other infrastructure

@docs/plan.md

**Principle**: Whatever subset of plan.md is "currently in scope" must be turnkey deployable before expanding scope.

### Definition of "Done"

**You are NOT done unless:**

1. You have turnkey `./bootstrap.sh` (the ONLY supported method)
2. That **reliably** results in everything in-scope functioning
3. **Without needing ANY further manual tweaks**
4. **All in-scope applications have working SSO authentication**

**Completion criteria:**

- `terraform destroy` → `./bootstrap.sh` → run all health checks
- **If ANY component is unhealthy, it does NOT work by definition**
- **If ANY in-scope application lacks functional SSO login, it does NOT work**
- No declaring "good enough" or aborting work on broken turnkey flow

**SSO Verification Requirements:**

Applications with SSO in scope must have:

- OIDC provider application created in Authentik
- Application-specific OIDC secrets synced to application namespace
- SSO login flow working end-to-end (user can log in via Authentik)
- Automated provisioning working (no manual user creation required)

**Only hand over as "it works" after full destroy→bootstrap→verify cycle passes.**

### Core Principles

1. **NO imperative patches** - All fixes must be encoded in configuration and committed properly
2. **Main development loop**: `destroy -> recreate -> check if valid`
   - See `docs/critical_dependencies.md` for dependency chain and bootstrap order
3. **Debugging vs. Implementation**:
   - **Debugging**: You CAN tinker with invalid/failed state (kubectl patches, manual commands) to understand what
     broke and learn how to fix declarative config
   - **Implementation**: All solutions MUST be declarative configuration changes, never manual fixes
   - **"The cluster works" ≠ DONE** - Getting broken state functioning via manual patches is NOT completion
4. **End-to-end declarative working config** - The outer true goal is always complete declarative automation

### Development Workflow

```bash
# Primary loop for all changes:
terraform destroy --auto-approve
./bootstrap.sh
# Verify: does it work end-to-end declaratively?
```

### SSO Integration Architecture

**Split Blueprint Pattern**: Authentik SSO integration uses a two-blueprint approach to handle namespace dependencies.

**Pattern Structure:**

1. **Provider Blueprint** (`authentik-blueprint-{app}-provider`)
   - Lives in `terraform/03-configuration/authentik-blueprints/`
   - Creates OIDC application in Authentik (authentik namespace)
   - Generates client ID and client secret
   - No dependency on target application namespace existing

2. **Secret Blueprint** (`authentik-blueprint-{app}-secret`)
   - Lives in `k8s/{app}/` with application manifests
   - Creates ExternalSecret in application namespace
   - Pulls OIDC credentials from Vault
   - Depends on: provider blueprint → app namespace created → secret blueprint

**Why This Pattern:**

- **Circular dependency prevention**: Application namespace doesn't exist until Flux creates it
- **Provider-first creation**: OIDC application must exist before secrets can be generated
- **Clean separation**: Infrastructure (Terraform) vs. Application (Flux GitOps)

## Example: Gitea SSO Integration

```text
terraform/03-configuration/authentik-blueprints/gitea-provider.yaml
  ↓ (creates OIDC app in Authentik, stores credentials in Vault)
k8s/gitea/ namespace creation by Flux
  ↓
k8s/gitea/authentik-blueprint-gitea-secret.yaml
  ↓ (ExternalSecret pulls from Vault)
k8s/gitea/helmrelease.yaml
  ↓ (consumes oidc-credentials secret for SSO config)
Gitea pod with working SSO
```

**Verification Commands:**

```bash
# Check provider blueprint applied
kubectl get terraform -n flux-system authentik-blueprint-gitea-provider

# Check secret exists in app namespace
kubectl get secret oidc-credentials -n gitea

# Check ExternalSecret synced
kubectl get externalsecret -n gitea

# Test SSO login flow (requires browser/agent access)
# Navigate to https://gitea.test-cluster.agentydragon.com
# Click "Sign in with OpenID Connect"
```

## Bootstrap Script - ONLY Supported Method

**CRITICAL**: The cluster MUST only be bootstrapped using `./bootstrap.sh`

### Why Bootstrap Script (Not Direct Terraform)

**Never run `terraform apply` directly.** The bootstrap script is required because:

1. **Preflight Validation**: Comprehensive checks before any infrastructure changes
   - Git working tree must be clean (Flux requirement)
   - Pre-commit validation (security, linting, format)
   - Terraform configuration validation
2. **Proper Error Handling**: Clear error messages and early failure detection
3. **Battle-tested Flow**: Proven sequence that prevents partial failure states
4. **Documentation**: Self-documenting deployment process

### Bootstrap Script Features

- **🔍 Preflight validation**: Git clean + pre-commit + terraform validate
- **⚡ Native provider deployment**: Talos → Cilium → Flux → Applications
- **🛡️ Terraform state keypair persistence**: Sealed secrets work across destroy/apply
- **📊 Clear progress reporting**: Phase-by-phase status updates
- **❌ Fail-fast behavior**: Stops immediately on any validation failure

### Usage

```bash
cd terraform/infrastructure
./bootstrap.sh
```

**That's it.** The script handles everything from validation to complete cluster deployment.

## Primary Development Loop

Main cycle: **destroy → recreate → check if valid**

If the result is broken/invalid, you may inspect and debug the live state to understand the failure. But the fix MUST be
committed configuration changes that make the next destroy→recreate cycle work properly.

### Cluster Disposability

**The cluster is completely disposable.** If it gets corrupted/broken, just `terraform destroy` it. Don't bother repairing
running state.

### Debugging vs. Completion Distinction

**Debugging a broken cluster:**

- You CAN tinker, patch, run manual kubectl commands
- Purpose: Learn WHY the declarative config failed
- Goal: Understand what needs to be fixed in committed configuration

**Completion criteria:**

- **"The cluster works" ≠ DONE**
- Getting current broken instance functioning via patches is NOT completion
- **DONE = teardown & bootstrap results in working cluster**
- Must pass: `terraform destroy && ./bootstrap.sh` → all components healthy

## SSH Access

**Proxmox Host:** `ssh root@atlas` - No password required (SSH keys configured)

**VPS (Public DNS):** `ssh root@agentydragon.com` - Runs PowerDNS secondary for public DNS

### VPS PowerDNS Access

The VPS runs PowerDNS in a Docker container as a secondary DNS server via AXFR from the cluster.

**Check zone contents:**

```bash
ssh root@agentydragon.com "docker exec powerdns pdnsutil list-zone test-cluster.agentydragon.com"
```

**Grep for specific records:**

```bash
ssh root@agentydragon.com "docker exec powerdns pdnsutil list-zone test-cluster.agentydragon.com | grep loki"
```

**Check container status:**

```bash
ssh root@agentydragon.com "docker ps | grep pdns"
```

**View container logs:**

```bash
ssh root@agentydragon.com "docker logs powerdns --tail 50"
```

## Talos CLI Access

- Run `talosctl` commands from cluster directory (direnv auto-loaded)
- Use `direnv exec /home/agentydragon/code/cluster talosctl` if running from other directories
- The direnv config automatically sets `TALOSCONFIG` path and provides talosctl via nix

## Working Directory

- Terraform layers:
  - `terraform/00-persistent-auth/` - Proxmox credentials, CSI tokens, sealed secrets keypair
  - `terraform/01-infrastructure/` - VMs, Talos, Cilium CNI
  - `terraform/02-services/` - Flux, core services, applications
  - `terraform/03-configuration/` - DNS, SSO configuration
- GitOps terraform: `terraform/gitops/` (tofu-controller managed)
- VM IDs: 1500-1502 (controlplane0-2), 2000-2001 (worker0-1)
- Node IPs: 10.0.1.x (controllers), 10.0.2.x (workers), 10.0.3.x (VIPs)

## Reference Code Location

**Base**: `/code` using `domain.tld/org/repo` pattern

**Key references:**

- `github.com/rgl/terraform-proxmox-talos` - Reference config, `./do init` builds custom images
- `github.com/longhorn/longhorn-charts` - Longhorn schemas at `charts/longhorn/values.yaml`
- `github.com/bank-vaults/bank-vaults` - Bank-Vaults operator source
- `github.com/fluxcd/flux2` - Flux CD examples
- `github.com/bpg/terraform-provider-proxmox` - Proxmox provider
- `github.com/siderolabs/terraform-provider-talos` - Talos provider

Use cloned repos as implementation ground truth.

## Key Files

- `talos.tf` - Talos machine configurations with Tailscale
- `proxmox.tf` - VM definitions
- `variables.tf` - Configuration variables
- `vault-secrets.tf` - Ansible vault integration via external data source

## Project Documentation Strategy

### docs/bootstrap.md

**Purpose**: ONLY straight-line sequence to recreate a functioning cluster from unpopulated Proxmox.

**Content**:

- Step-by-step instructions for cold-starting the Talos cluster from nothing
- Complete deployment procedures (terraform, CNI, applications, external connectivity)
- Basic verification steps only (see docs/troubleshooting.md for health checks)
- **NO troubleshooting** (would be too verbose - half a megabyte)

**Maintenance**: Continuously update to reflect current state. Changes require bootstrap.md updates.

### docs/operations.md

**Purpose**: Day-to-day cluster management procedures including scaling, maintenance, and troubleshooting.

**Content**:

- Node operations (adding, removing, restarting)
- System diagnostics and VM console management
- Comprehensive troubleshooting guide with symptoms and solutions
- Reference information (IP assignments, file locations)

**Maintenance**: Updated when operational procedures change or new troubleshooting scenarios are discovered.

### docs/troubleshooting.md

**Purpose**: Fast-path diagnostic checklist for common cluster issues.

**Content**:

- Quick health checks for core components
- **Storage troubleshooting** (Proxmox CSI is known tricky - SealedSecret decryption issues)
- GitOps debugging commands
- Stable SealedSecret keypair verification
- Common recovery actions and known issues

**Maintenance**: Updated as new issues are discovered and resolved.

### docs/plan.md

**Purpose**: Describes high-level goals we want to implement, lists what we finished, and what remains to be done as items.

**Content**:

- Project overview and architecture decisions
- Completed features with status markers ([x])
- Remaining tasks as checkbox items ([ ])
- Partially complete tasks as ([ ] PARTIAL)
- Design documents for planned features
- Strategic technical decisions and rationale

**Maintenance**: Tracks project roadmap. Move completed items to "Achieved" sections, add new goals.

## Key Principles

1. **DECLARATIVE FIRST** - All configuration must work via destroy->recreate cycle without manual intervention
2. **docs/bootstrap.md is always actionable** - anyone should be able to follow it and get a working cluster
3. **docs/plan.md is strategic** - focuses on what we're building and why
4. **Keep both in sync** - when implementation is complete, move details from docs/plan.md to docs/bootstrap.md
5. **Document current state accurately** - especially important for infrastructure that changes over time
6. **Debug broken state to understand, but fix via committed config** - Never leave manual patches as the solution

## Command Execution Context

**All kubectl, talosctl, kubeseal, flux, and helm commands** assume cluster directory execution or `direnv exec .`.

This provides consistent tool versions (nix-managed) and automatic KUBECONFIG/TALOSCONFIG environment variables.

## Terraform Timeout Configuration

**IMPORTANT**: When running `terraform apply` or `terraform destroy`, always use the Bash tool's `timeout` parameter
set to 600000ms (10 minutes) to prevent premature timeout during long cluster provisioning operations.

Example:

```json
{
  "command": "terraform apply -auto-approve",
  "timeout": 600000,
  "description": "Apply terraform with maximum timeout"
}
```

Never use the `timeout` command prefix - use the tool's built-in timeout parameter instead.

## Bootstrap Timing Reference

**Observed timings for terraform apply phases (2026-01-03 hybrid VPS+Proxmox cluster):**

| Phase                      | Typical Duration | Notes                                    |
| -------------------------- | ---------------- | ---------------------------------------- |
| Proxmox VM creation        | 30-35s           | Per VM, parallel                         |
| Hetzner VPS creation       | 55s-1m15s        | Per VPS, parallel                        |
| VPS Talos config apply     | 20-30s           | Per VPS                                  |
| Proxmox Talos config apply | 7-9 min          | Per node, slower due to disk import      |
| talos_machine_bootstrap    | <1s              | Triggers etcd bootstrap                  |
| K8s API wait               | 5-10 min         | wait-for-k8s-api.sh, 60 attempts × 10s   |
| Cilium installation        | 30-40s           | helm_release.cilium_bootstrap            |
| Nodes Ready wait           | ~1s              | After Cilium, nodes become Ready quickly |
| hcloud-csi installation    | 15-20s           | Hetzner CSI driver                       |

**Total estimated bootstrap time**: 15-20 minutes from terraform apply start to all nodes Ready.

**Key slowest phases**:

1. **Proxmox Talos config apply** (7-9 min per node) - Limited by disk import from downloaded qcow2
2. **K8s API wait** (5-10 min) - Waiting for all control plane nodes to be ready

**When monitoring bootstrap**: If you're past 10 minutes without Cilium installed, check terraform state for what's
blocking (usually Proxmox config apply or K8s API wait).

## Checklist

- **Before making changes**: Read docs/bootstrap.md to understand current working state
- **After completing work**: Update docs/bootstrap.md with new procedures if they change the bootstrap sequence
- **When planning**: Use docs/plan.md to understand goals and add new tasks
- **When finishing features**: Mark items complete in docs/plan.md and ensure docs/bootstrap.md reflects the new capabilities
- **When diagnosing issues**: Use docs/troubleshooting.md fast-path commands first before deep debugging

This ensures the documentation serves both as operational procedures (docs/bootstrap.md) and project management (docs/plan.md).

## Common Issues and Resolutions

@docs/troubleshooting.md

@docs/archive/SECRET_SYNCHRONIZATION_ANALYSIS.md

## Troubleshooting Priority

**Always use docs/troubleshooting.md first** when cluster components aren't working:

1. **Fast-path health checks** - Quick status commands for all core components
2. **Known tricky components** - Proxmox CSI storage issues, SealedSecret decryption problems
3. **Common recovery actions** - Controller restarts, forced reconciliation
4. **Only then** proceed to deeper investigation if fast-path doesn't resolve the issue

## Secrets Management

@docs/secrets.md
