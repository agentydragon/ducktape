# Haku sandbox image — the toolchain haku-console's `sandbox` server hands out.
#
# This is an in-cluster EXEC TARGET, not an agent-loop runtime: the agent execs
# `bazel run //cli:...` / `bazel test //...` in here (through Console) against a
# git-synced haku-state checkout. haku-state builds LOCALLY (its .bazelrc is
# --spawn_strategy=local, no BuildBuddy/RBE — the source must never leave the
# cluster), so this image bakes the full local toolchain, not an RBE client.
#
# Deliberately minimal (the haku-ci warm-builder image, if ever built under
# haku/plans/ci_warm_bazel_server.md, is a separate image):
# bazelisk + a JDK (keytool + the Bazel server JVM) + git/curl/openssl + kubectl + ruff. NO
# nix, NO docker (bazel's //ui:image uses hermetic rules_oci), NO browser/Rust/GHC deps.
# kubectl earns its place because the pod runs as the `haku` ServiceAccount (full CRUD
# in haku-sandbox) and haku-state's run plumbing shells out to it — tools/ci_wait.sh
# reads the Forgejo password from a Secret, tools/plaid_q.sh runs the query pod, and
# cli/k8s_secrets.py backs `haku console`. Without it those all fail at their first line.
# python3 (FULL, not python3-minimal) likewise: tools/ci_wait.sh parses the Forgejo runs API
# with an inline python3 snippet, and its absence produced a FALSE GREEN (empty parse -> every
# `[ ]` integer test errors -> fall through to "all runs green", exit 0) until haku-state PR #41
# guarded it. It was python3-minimal until 2026-07-25, which was the worst of both worlds: a
# live run measured `json`, `urllib.request`, `http.client`, `shutil`, `difflib`, `dataclasses`,
# and `sqlite3` all missing, so a `command -v python3` check passes and the import dies at
# runtime. That also breaks the sandbox's actual authoring motion — an agent driving this box
# through `exec_sandbox` edits files with heredoc'd python, where one stray `import shutil`
# kills the whole script. Stdlib only — the run's own python deps come from Bazel, never here.
# jq: same class of gap, for the shell-side JSON the CLI and k8s recipes produce.
# tea: haku-state's `procedures/code_changes.md` routes code changes through Forgejo PRs, which
# is a `tea` workflow; without it that procedure has no non-REST path from inside the sandbox.
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
      python3 jq \
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

# tea, pinned to the version the repo's devshell and codex-pod image already carry
# (nixpkgs `tea` 0.14.0), so the Forgejo CLI behaves the same wherever an agent runs it.
ARG TEA_VERSION=0.14.0
RUN curl -fsSL -o /usr/local/bin/tea \
      "https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-amd64" \
    && chmod +x /usr/local/bin/tea \
    && tea --version

# ruff pinned to the version haku-state's lint workflow installs
# (.forgejo/workflows/lint.yaml: `pip install ruff==...`) — a DIFFERENT pin there and here
# means the sandbox greenlights code CI then rejects, which is worse than not having it.
# Earns its place because lint is the one CI gate the sandbox couldn't reproduce: ruff is
# not a Bazel target (see that workflow's header for why), and full `python3` above still
# ships no pip, so an agent working haku-state could only eyeball line lengths and
# blank-line runs. That cost three consecutive red PRs (E501, E303, E402) before this was
# baked in. Standalone binary, same shape as the bazelisk/kubectl/tea pins above.
ARG RUFF_VERSION=0.15.20
RUN curl -fsSL "https://github.com/astral-sh/ruff/releases/download/${RUFF_VERSION}/ruff-x86_64-unknown-linux-gnu.tar.gz" \
      | tar -xz -C /usr/local/bin --strip-components=1 --wildcards '*/ruff' \
    && ruff --version

# The whole reviewed per-claim bootstrap (egress CA, git identity + credentials, the
# haku-state checkout). Console's configured bootstrap.script is just a call
# to this, so the logic stays in a lintable file instead of a bash blob in config YAML.
COPY haku-sandbox-setup.sh /usr/local/bin/haku-sandbox-setup.sh
RUN chmod +x /usr/local/bin/haku-sandbox-setup.sh

# Non-root, uid 1000 (matches the agent-workspaces lane + the SandboxTemplate
# securityContext). /workspace is the writable working root; ~/.cache is a PVC.
RUN useradd --create-home --uid 1000 --shell /bin/bash workspace \
    && mkdir -p /workspace && chown workspace:workspace /workspace
USER workspace
WORKDIR /workspace
ENV HOME=/home/workspace
