# Self-Hosted CI & RBE: GitLab FOSS + BuildBuddy Firecracker

## Current State

| Component                 | Implementation                                                   |
| ------------------------- | ---------------------------------------------------------------- |
| **CI Orchestrator**       | GitHub Actions (`.github/workflows/`)                            |
| **Build Execution**       | BuildBuddy Cloud RBE via `bb remote`                             |
| **Self-hosted execution** | GitHub Actions self-hosted runners (GHA-runners)                 |
| **Workload Isolation**    | OCI containers (default), Firecracker (experimental on cloud)    |
| **SSO/Auth**              | GitHub SSO via OAuth provider                                    |
| **Secrets**               | GitHub Secrets (`BUILDBUDDY_API_KEY`, GHA secrets)               |
| **Artifact storage**      | GitHub Actions artifacts, ghcr.io container registry             |
| **Billing**               | GitHub Actions (free), BuildBuddy Cloud (per-invocation + cache) |

## Target State

| Component                 | Target Implementation                                                  |
| ------------------------- | ---------------------------------------------------------------------- |
| **CI Orchestrator**       | GitLab FOSS (`.gitlab-ci.yml`)                                         |
| **Build Execution**       | BuildBuddy RBE Self-Hosted via `bb remote`                             |
| **Self-hosted execution** | GitLab Runner (self-hosted in cluster)                                 |
| **Workload Isolation**    | Firecracker MicroVMs on self-hosted executors                          |
| **VM Snapshot/Resume**    | Firecracker snapshotting with runner recycling                         |
| **SSO/Auth**              | GitLab: OmniAuth → Authentik OIDC; BuildBuddy: Authentik reverse proxy |
| **Secrets**               | GitLab Variables, BuildBuddy local config                              |
| **Artifact storage**      | GitLab CI/CD artifacts, built-in registry (or keep ghcr.io)            |
| **Billing**               | Infra only (your cluster costs)                                        |

---

# Part 1: BuildBuddy Licensing Analysis

## Editions Comparison

| Feature                                  | FOSS (Free/Open Source) | Enterprise (Licensed)     |
| ---------------------------------------- | ----------------------- | ------------------------- |
| **Self-hosted app server**               | Yes                     | Yes                       |
| **BuildBuddy Cloud RBE**                 | Yes                     | Yes                       |
| **Self-hosted executor pool**            | No                      | Yes                       |
| **Firecracker on self-hosted executors** | No                      | Yes                       |
| **Firecracker on cloud executors**       | Yes (experimental)      | Yes                       |
| **OIDC Auth**                            | No                      | Yes (Okta, Auth0, GSuite) |
| **BuildBuddy API**                       | No                      | Yes                       |
| **Custom Docker images for RBE**         | No                      | Yes                       |
| **Configurable TTL**                     | No                      | Yes                       |
| **HA configurations**                    | No                      | Yes                       |

**Critical blocker:** Self-hosted Firecracker RBE requires Enterprise license. FOSS can self-host the app server only — executor pool is Enterprise-only.

## Enterprise Feature Relevance

| Feature                                  | Description                            | Relevance                          |
| ---------------------------------------- | -------------------------------------- | ---------------------------------- |
| **Self-hosted executor pool**            | Deploy your own executor pool          | Critical                           |
| **Firecracker on self-hosted executors** | MicroVM isolation                      | Critical                           |
| **OIDC Auth**                            | Integrate with Authentik, Okta, GSuite | Nice-to-have (reverse proxy works) |
| **BuildBuddy API**                       | Programmatic access to build results   | Nice-to-have                       |
| **Custom Docker images**                 | Use your own RBE worker images         | Already doing this                 |
| **Configurable TTL**                     | Set build result retention             | Useful but not critical            |
| **HA configurations**                    | High availability setups               | Nice-to-have                       |

## License Deployment

```yaml
# helmrelease.yaml
valuesFrom:
  - kind: Secret
    name: buildbuddy-enterprise-license
    valuesKey: license-key
    targetPath: config.app.license_key
```

License key stored in Kubernetes Secret (SOPS-encrypted recommended).

## Alternative RBE Providers

- [GitHub Actions Cache Service](https://github.com/features/actions-cache)
- [Bazel Remote Execution (Google)](https://bazel.build/remote-execution) — mature but cloud-only
- [BuildFarm](https://buildfarm.io/) — open-source RBE
- [BuildGrid](https://buildgrid.io/) — open-source RBE

BuildBuddy's Firecracker + snapshot/resume is the most sophisticated option. Building custom executors (implementing the Bazel RBE protocol yourself) is extremely complex and not recommended.

---

# Part 2: GitLab FOSS Migration

## GitLab FOSS vs GitHub Actions

| Component               | GitHub Actions               | GitLab FOSS                     | What Changes                      |
| ----------------------- | ---------------------------- | ------------------------------- | --------------------------------- |
| **CI Orchestrator**     | `.github/workflows/`         | `.gitlab-ci.yml`                | Convert workflow syntax           |
| **Self-hosted runners** | GHA-runners (`concurrent:1`) | GitLab Runner (same model)      | Deploy `gitlab-runner` in cluster |
| **Triggers**            | `on: [push, pull_request]`   | `workflow:rules`, `only/except` | Update trigger syntax             |
| **Jobs**                | `jobs: <name>`               | `stages:`, `job-name:`          | Restructure job hierarchy         |
| **Matrix**              | `strategy.matrix`            | `parallel:matrix`               | Convert matrix syntax             |
| **Caching**             | `actions/cache`              | Built-in `cache:`               | Remove actions/cache              |
| **Docker**              | `container:`, `services:`    | `image:`, `services:`           | Update Docker syntax              |
| **Remote execution**    | Call `bbr` in steps          | Call `bbr` in script            | Same invocation                   |

### Workflow Conversion Example

```yaml
# GitHub Actions (.github/workflows/ci.yml)
jobs:
  build:
    runs-on: [self-hosted, linux]
    steps:
      - name: Build
        run: bbr build //...
      - name: Test
        run: bbr test //...

# GitLab CI (.gitlab-ci.yml)
stages:
  - build
  - test

build:
  stage: build
  tags: [self-hosted]
  script:
    - bbr build //...

test:
  stage: test
  tags: [self-hosted]
  script:
    - bbr test //...
  needs: [build]
```

## GitLab Runner Deployment

```bash
helm repo add gitlab https://charts.gitlab.io

cat > gitlab-runner-values.yaml <<EOF
gitlabUrl: https://gitlab.allegedly.works
runnerRegistrationToken: <your-runner-token>
concurrent: 1
EOF

helm install gitlab-runner gitlab/gitlab-runner \
  --namespace gitlab-runner --create-namespace \
  --values gitlab-runner-values.yaml
```

---

# Part 3: BuildBuddy Self-Hosted Deployment

## Cloud vs Self-Hosted

| Component              | BuildBuddy Cloud                          | BuildBuddy Self-Hosted           | What Changes                      |
| ---------------------- | ----------------------------------------- | -------------------------------- | --------------------------------- |
| **RBE Server**         | Managed (`remote.buildbuddy.io`)          | Self-hosted in k8s               | Deploy via Helm                   |
| **Executor Pool**      | Managed Linux executors                   | Self-hosted pool (3+ replicas)   | Configure replicas, resources     |
| **Workload Isolation** | OCI (default), Firecracker (experimental) | OCI, Docker, Podman, Firecracker | Enable Firecracker on executors   |
| **VM Snapshot/Resume** | Firecracker snapshotting                  | Same technology                  | No change                         |
| **Runner Recycling**   | `recycle-runner` exec property            | Same                             | No change                         |
| **Remote Bazel**       | `bb remote` CLI                           | `bb remote` CLI                  | Works identically                 |
| **Cache**              | Managed remote cache                      | Self-hosted Redis                | Optional: own cache or keep cloud |
| **Billing**            | Per-invocation + cache storage            | Infra only                       | Eliminates cloud billing          |

## Enterprise Helm Deployment

```bash
helm repo add buildbuddy https://helm.buildbuddy.io

cat > buildbuddy-values.yaml <<EOF
executor:
  enabled: true
  replicas: 3
  resources:
    requests:
      cpu: "2"
      memory: "8Gi"
    limits:
      cpu: "4"
      memory: "16Gi"
redis:
  enabled: true
config:
  remote_execution:
    enable_remote_exec: true
EOF

helm install buildbuddy buildbuddy/buildbuddy-enterprise \
  --namespace buildbuddy --create-namespace \
  --values buildbuddy-values.yaml
```

FOSS on-prem app server (no executor pool): `gcr.io/flame-public/buildbuddy-app-onprem:latest`, StatefulSet with 10 GiB PVC, ports HTTP (8080) + gRPC (1985).

## Enabling Firecracker on Executors

Create a DaemonSet to configure Firecracker on executor nodes:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: buildbuddy-firecracker-setup
  namespace: buildbuddy
spec:
  selector:
    matchLabels:
      app: buildbuddy-firecracker-setup
  template:
    metadata:
      labels:
        app: buildbuddy-firecracker-setup
    spec:
      hostPID: true
      initContainers:
        - name: enable-firecracker
          image: alpine:latest
          securityContext:
            privileged: true
          command: ["/bin/sh", "-c"]
          args:
            - |
              apk add --no-cache iproute2 iptables
              sysctl -w vm.unprivileged_userfaultfd=1
              echo 1 >/proc/sys/net/ipv4/ip_forward
              chmod 666 /dev/kvm
      containers:
        - name: jailer-setup
          image: gcr.io/flame-public/buildbuddy-executor-enterprise:latest
          securityContext:
            privileged: true
          command: ["/bin/sh", "-c"]
          args:
            - |
              groupadd -f -r cgroups || true
              usermod -a -G cgroups $(whoami) || true
              mkdir -p /sys/fs/cgroup/cpuset/firecracker
              chmod g+rw /sys/fs/cgroup/cpuset/firecracker
              mkdir -p /sys/fs/cgroup/firecracker
              chmod g+rw /sys/fs/cgroup/firecracker
              chown -R $(whoami):cgroups /sys/fs/cgroup/cpuset/firecracker
              chown -R $(whoami):cgroups /sys/fs/cgroup/firecracker
              chmod -R g+rw /sys/fs/cgroup/cpuset/firecracker
              chmod -R g+rw /sys/fs/cgroup/firecracker
              setfacl -m u:$(whoami):rw /dev/kvm
              echo "Firecracker setup complete"
```

## Bazel Configuration for Self-Hosted Executor

```python
# .bazelrc
build --enable_platform_specific_config
build:remote --repo_env=BES_REMOTE_EXECUTION=buildbuddy
build:remote --remote_executor=grpcs://buildbuddy.buildbuddy.svc.cluster.local:1985
build:remote --remote_cache=grpcs://buildbuddy.buildbuddy.svc.cluster.local:1986
build:remote --host_platform=@buildbuddy_config//platforms:linux-remote-firecracker
```

Platform definition:

```python
# @buildbuddy_config//platforms/BUILD
platform(
    name = "linux-remote-firecracker",
    constraint_values = [
        "@bazel_tools//tools/os:linux",
        "@bazel_tools//tools/cpu:x86_64",
    ],
    exec_properties = {
        "container-image": "docker://ghcr.io/agentydragon/rbe-worker",
        "workload-isolation-type": "firecracker",
    },
    parents = ["@buildbuddy_config//platforms:linux-remote"],
)
```

## Firecracker Features

### Runner Recycling (Warm VMs)

```python
sh_test(
    name = "docker_test",
    srcs = ["docker_test.sh"],
    exec_properties = {
        "test.workload-isolation-type": "firecracker",
        "test.recycle-runner": "true",
        "test.init-dockerd": "true",
    },
)
```

First action boots a fresh Firecracker VM and starts Docker. Subsequent actions with matching `exec_properties` resume from a warm snapshot.

### Docker-in-Firecracker

```bash
# postgres_test.sh — Docker containers persist between test runs
docker container inspect pg-server >/dev/null 2>&1 || \
  docker run -d --name pg-server -e POSTGRES_PASSWORD=secret postgres:15
docker exec pg-server psql -U postgres -c "DROP DATABASE IF EXISTS testdb;"
docker exec pg-server psql -U postgres -c "CREATE DATABASE testdb;"
docker exec pg-server psql -U postgres testdb -f test.sql
```

---

# Part 4: Firecracker Technical Requirements

## Cluster Prerequisites

| Requirement        | Status in Atlas Cluster | Notes                            |
| ------------------ | ----------------------- | -------------------------------- |
| **KVM support**    | Yes (Proxmox workers)   | `/dev/kvm` via KVM device plugin |
| **cgroups v2**     | Yes (Talos Linux)       | Required for resource isolation  |
| **Kernel >= 4.15** | Yes                     | For cgroups v2 + userfaultfd     |

## Resource Requirements (Per Executor)

| Resource    | Minimum | Recommended (with Firecracker)      |
| ----------- | ------- | ----------------------------------- |
| **CPU**     | 2 vCPUs | 4 vCPUs                             |
| **RAM**     | 4 GiB   | 8-16 GiB (accounts for VM overhead) |
| **Storage** | 10 GiB  | 50 GiB (for snapshots)              |

Memory overhead: VM image (~2-4 GiB) + running VM memory (4-8 GiB) + snapshot storage (full RAM + disk, ~66 GiB for 16 GiB RAM + 50 GiB disk).

## System Configuration

From `enable_local_firecracker.sh`:

```bash
# Kernel parameters
sysctl -w vm.unprivileged_userfaultfd=1

# Cgroups
mkdir -p /sys/fs/cgroup/cpuset/firecracker
mkdir -p /sys/fs/cgroup/firecracker
chmod g+rw /sys/fs/cgroup/cpuset/firecracker
chmod g+rw /sys/fs/cgroup/firecracker

# KVM access
setfacl -m u:<user>:rw /dev/kvm

# Networking
echo 1 >/proc/sys/net/ipv4/ip_forward
iptables -t nat -A POSTROUTING -o <primary-iface> -j MASQUERADE
iptables -A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

# Jailer capabilities
setcap CAP_MKNOD,CAP_SYS_ADMIN,CAP_NET_ADMIN+eip <jailer-path>
```

Production deployment: wrap in DaemonSet/initContainer pattern (see Part 3).

## Firecracker Versions

From `/code/github.com/buildbuddy-io/buildbuddy/`:

- **x86_64:** v1.13.0-with_clock_reset_patch-20251029
- **ARM64:** v1.11.0-aarch64

BuildBuddy applies patches to Firecracker for better integration.

---

# Part 5: SSO Integration

## GitLab + Authentik (OmniAuth)

GitLab FOSS supports OmniAuth pluggable authentication.

### Authentik Provider Setup

1. Create OAuth2/OIDC Provider in Authentik:
   - Redirect URI: `https://gitlab.allegedly.works/users/auth/authentik/callback`
   - Scopes: `openid`, `profile`, `email`

2. Configure GitLab OmniAuth:

```yaml
# gitlab.rb (Omnibus) or via Helm values
gitlab_rails['omniauth_enabled'] = true
gitlab_rails['omniauth_providers'] = [
  {
    name: 'authentik',
    app_id: ENV['GITLAB_AUTHENTIK_CLIENT_ID'],
    app_secret: ENV['GITLAB_AUTHENTIK_CLIENT_SECRET'],
    args: {
      scope: 'openid profile email',
      discovery: true,
      issuer: 'https://authentik.allegedly.works/application/o/gitlab/'
    }
  }
]
```

### SAML Alternative

```yaml
gitlab_rails['omniauth_providers'] = [
  {
    name: 'saml',
    args: {
      assertion_consumer_service_url: 'https://gitlab.allegedly.works/users/auth/saml/callback',
      idp_cert_fingerprint: ENV['SAML_IDP_CERT_FINGERPRINT'],
      idp_sso_target_url: ENV['SAML_IDP_SSO_TARGET_URL'],
      issuer: 'gitlab'
    }
  }
]
```

## BuildBuddy + Authentik (Reverse Proxy)

Neither FOSS nor Enterprise BuildBuddy has built-in OIDC. Use Authentik reverse proxy via Gateway API:

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: HTTPRoute
metadata:
  name: buildbuddy
  namespace: buildbuddy
spec:
  parentRefs:
    - name: main
  hostnames:
    - buildbuddy.allegedly.works
  rules:
    - filters:
        - type: ExtensionRef
          extensionName: oauth2
      backendRefs:
        - name: buildbuddy-app-service
          port: 80
```

Limitations: protects HTTP/gRPC access only; the app itself has no user auth concept beyond this.

## Runner Authentication

GitLab runners and BuildBuddy executors authenticate via tokens, not SSO. SSO applies to user authentication only.

---

# Part 6: Combined Migration Strategy

## Phase 1: Parallel Operations

1. **Keep existing active:** GitHub Actions running, BuildBuddy Cloud active, GHA-runners in cluster
2. **Deploy new components:** GitLab FOSS (Helm via Flux), BuildBuddy Enterprise (Helm in cluster), GitLab Runner (1-2 replicas), BuildBuddy Executors (1-2 replicas with Firecracker)
3. **Configure Authentik SSO:** Create OAuth2/OIDC provider for GitLab; optional reverse proxy for BuildBuddy
4. **Test pilots:** Point single GHA workflow to GitLab Runner; point single Bazel target to BuildBuddy self-hosted executor; verify Firecracker, Docker-in-FC, runner recycling

## Phase 2: Workflow Migration

1. Convert `.github/workflows/*.yml` → `.gitlab-ci.yml`
2. Test each workflow in parallel with GHA
3. Migrate secrets from GitHub Secrets → GitLab Variables
4. Update `.bazelrc` to use self-hosted BuildBuddy executor
5. Verify invocation tracking (`bbapi`), Firecracker isolation, and cache hit rates

## Phase 3: BuildBuddy Integration

1. Ensure GitLab runners have `BUILDBUDDY_API_KEY` variable
2. Verify `bbr` commands work from GitLab Runner
3. Choose cache strategy: self-hosted Redis, keep BuildBuddy Cloud cache (hybrid), or `--disk_cache`
4. Choose registry: GitLab built-in or keep ghcr.io

## Phase 4: Artifact & Webhook Migration

1. Migrate GitHub Actions artifacts → GitLab CI/CD artifacts
2. Flux image automation: use GitLab system hooks (native) or deploy GitLab webhook receiver

## Phase 5: Cutover

1. Disable GitHub workflows, remove GHA-runners deployment, clean up GitHub secrets
2. Remove `BUILDBUDDY_API_KEY` from secrets, confirm all `.bazelrc` point to self-hosted executor, cancel BuildBuddy Cloud subscription

---

# Part 7: Resource Requirements

## Combined Deployment (GitLab + BuildBuddy)

| Component               | Minimum                     | Recommended                 |
| ----------------------- | --------------------------- | --------------------------- |
| **GitLab Runner**       | 1 replica, 0.5 CPU, 1Gi RAM | 2 replicas, 1 CPU, 2Gi RAM  |
| **BuildBuddy App**      | 1 replica, 1 CPU, 4Gi RAM   | 2 replicas, 2 CPU, 8Gi RAM  |
| **BuildBuddy Executor** | 1 replica, 2 CPU, 8Gi RAM   | 3 replicas, 4 CPU, 16Gi RAM |
| **Redis**               | 1 CPU, 5Gi RAM              | 2 CPU, 10Gi RAM (HA)        |
| **Storage**             | 20Gi PVC                    | 50Gi PVC (snapshots)        |

**Total:** ~4-12 CPU, 18-40Gi RAM for full deployment.

---

# Part 8: Feature Comparisons

## GitLab FOSS Limitations

| Missing Feature                 | Impact                             | Workaround                    |
| ------------------------------- | ---------------------------------- | ----------------------------- |
| **Group-level SAML**            | Minimal — project-level sufficient | Use Authentik OIDC            |
| **External CI integrations**    | N/A — we use GitLab CI             | —                             |
| **Full Kubernetes integration** | Basic                              | Manual Flux config works fine |

## BuildBuddy FOSS Limitations

| Missing Feature               | Impact                                         | Workaround                            |
| ----------------------------- | ---------------------------------------------- | ------------------------------------- |
| **Self-hosted executor pool** | Critical — need Enterprise for Firecracker RBE | Buy Enterprise license                |
| **Built-in OIDC/SAML SSO**    | Nice-to-have                                   | Authentik reverse proxy (Gateway API) |

## Secret Management

| Aspect              | GitHub Actions                        | GitLab FOSS                  |
| ------------------- | ------------------------------------- | ---------------------------- |
| **Scope**           | Repository, Organization, Environment | Project, Group, Environment  |
| **Masking in logs** | Automatic (`::add-mask::`)            | Automatic (masked variables) |
| **File secrets**    | Not supported (env vars only)         | Supported (`type: file`)     |

---

# Part 9: Decision Framework

## Go Ahead with Full Migration if

- Want full self-hosted CI/CD (no dependency on GitHub or BuildBuddy Cloud)
- Comfortable converting GitHub Actions → GitLab CI syntax and maintaining BuildBuddy upgrades (Helm)
- Have KVM-enabled nodes (or can provision them)
- Want full data sovereignty (nothing leaves cluster)
- Want to eliminate BuildBuddy Cloud billing
- Build invocation volume is high enough that Enterprise license cost is less than monthly cloud billing

## Stay with BuildBuddy Cloud (Current Stack) if

- Build invocation volume is moderate (hundreds/month, per-invocation billing acceptable)
- Prefer managed service (no executor pool maintenance)
- No data sovereignty requirement
- Enterprise license cost is prohibitive
- Team capacity is limited (no time to maintain executor pool)

## Hybrid Approaches

### Option A: GitLab + BuildBuddy Cloud

Self-hosted CI, managed RBE. Benefit: migrate CI without needing Enterprise license.

### Option B: GitHub Actions + BuildBuddy Self-Hosted

Managed CI, self-hosted builds. Benefit: get Firecracker isolation without CI migration.

### Option C: FOSS App + Cloud Executors

Deploy FOSS BuildBuddy app server in cluster (data locality), use BuildBuddy Cloud executors (Firecracker available). FOSS license only, but still per-invocation billing. Hybrid complexity.

### Option D: Gradual Cutover

Start with pilot deployments (1-2 workflows/targets), migrate incrementally, keep cloud as fallback.

## Recommended Path

**Near-term (stay with BuildBuddy Cloud):** Already working. Firecracker available experimentally. No additional licensing cost. Add Authentik reverse proxy for SSO if needed.

**Future (evaluate Enterprise):** Request quote from BuildBuddy. Compare Enterprise license + infrastructure costs (3+ executors @ 16 GiB RAM) vs current BuildBuddy Cloud monthly billing. Factor in maintenance time vs managed service. Self-host if monthly cloud cost exceeds break-even.

---

# References

- BuildBuddy Pricing: https://buildbuddy.io/pricing
- BuildBuddy FOSS: https://github.com/buildbuddy-io/buildbuddy
- BuildBuddy Enterprise Helm: https://github.com/buildbuddy-io/buildbuddy-helm
- Firecracker MicroVMs: https://github.com/firecracker-microvm/firecracker
- GitLab OmniAuth: https://docs.gitlab.com/ee/integration/authentik.html
- GitLab Runner: https://docs.gitlab.com/runner/
- BuildBuddy Firecracker Setup: `/code/github.com/buildbuddy-io/buildbuddy/tools/enable_local_firecracker.sh`
- BuildBuddy RBE Firecracker: `/code/github.com/buildbuddy-io/buildbuddy/docs/rbe-microvms.md`
- Remote Runner Intro: `/code/github.com/buildbuddy-io/buildbuddy/docs/remote-runner-introduction.md`
- BuildBuddy Enterprise Docs: `/code/github.com/buildbuddy-io/buildbuddy/docs/enterprise.md`
