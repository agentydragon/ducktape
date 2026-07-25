# Haku sandbox image — the toolchain the sandbox-provisioning MCP hands out.
#
# This is an in-cluster EXEC TARGET, not an agent-loop runtime: the agent execs
# `bazel run //cli:...` / `bazel test //...` in here (through the MCP) against a
# git-synced haku-state checkout. haku-state builds LOCALLY (its .bazelrc is
# --spawn_strategy=local, no BuildBuddy/RBE — the source must never leave the
# cluster), so this image bakes the full local toolchain, not an RBE client.
#
# Deliberately minimal (cf. haku/plans/ci_warm_bazel_server.md "Builder image"):
# bazelisk + a JDK (keytool + the Bazel server JVM) + git/curl/openssl + kubectl. NO
# nix, NO docker (bazel's //ui:image uses hermetic rules_oci), NO browser/Rust/GHC deps.
# kubectl earns its place because the pod runs as the `haku` ServiceAccount (full CRUD
# in haku-sandbox) and haku-state's run plumbing shells out to it — tools/ci_wait.sh
# reads the Forgejo password from a Secret, tools/plaid_q.sh runs the query pod, and
# cli/k8s_secrets.py backs `haku console`. Without it those all fail at their first line.
# Language toolchains (CPython, Node/pnpm) are fetched hermetically by Bazel, but the
# C/C++ toolchain is NOT: haku-state's cc rules resolve through Bazel's local_config_cc
# autoconf, which probes for a system gcc/g++. So build-essential is baked in — without
# it any target that pulls the cc toolchain (e.g. //cli:bookmark) fails analysis with
# "Cannot find gcc or CC".
#
# Built by ducktape CI (.github/workflows/haku-sandbox-image.yml) and
# pushed to the Forgejo registry — a trusted, operator-reviewed substrate. Haku's
# method rides in only as the haku-state checkout, run via `bazel run`, bounded by
# the pod's operator-owned creds/egress/RBAC (see cluster/docs security model).
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssl \
      default-jre-headless \
      build-essential \
      procps less \
    && rm -rf /var/lib/apt/lists/*

# bazelisk pin matches haku-state's CI runner (.forgejo/actions/bazel-runner-setup):
# it reads .bazelversion (-> Bazel 8.6.0) and bundles a JDK for the Bazel server.
ARG BAZELISK_VERSION=v1.25.0
RUN curl -fsSL -o /usr/local/bin/bazel \
      "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-linux-amd64" \
    && chmod +x /usr/local/bin/bazel

# kubectl pinned to the cluster's control-plane minor (1.35.x); the ±1-minor skew policy
# also covers the 1.36 worker nodes. Bump when the control plane moves.
ARG KUBECTL_VERSION=v1.35.1
RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

COPY haku-sandbox-setup.sh /usr/local/bin/haku-sandbox-setup.sh
RUN chmod +x /usr/local/bin/haku-sandbox-setup.sh

# Non-root, uid 1000 (matches the agent-workspaces lane + the SandboxTemplate
# securityContext). /workspace is the writable working root; ~/.cache is a PVC.
RUN useradd --create-home --uid 1000 --shell /bin/bash workspace \
    && mkdir -p /workspace && chown workspace:workspace /workspace
USER workspace
WORKDIR /workspace
ENV HOME=/home/workspace
