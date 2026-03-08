#!/usr/bin/env bash
# Build a real NixOS Docker image and run Bazel inside it.
#
# The image uses nixpkgs.lib.nixosSystem (boot.isContainer = true) to produce
# a genuine NixOS filesystem. Built via nix-build of image.nix.
#
# Usage:
#   ./run.sh              # Build image, drop into interactive shell
#   ./run.sh build        # Only build the image
#   ./run.sh test         # Build image and run bazel build+test
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="ducktape-nixos-bazel"

build_image() {
  echo "=== Building NixOS Docker image ==="

  if command -v nix-build &>/dev/null; then
    echo "Using local nix-build..."
    nix-build "$SCRIPT_DIR/image.nix" -o "$SCRIPT_DIR/result"
  else
    echo "No local nix found, building inside nixos/nix container..."

    local -a docker_build_args=(
      --rm
      --network=host
      -v "$SCRIPT_DIR:/src:ro"
      -v "$SCRIPT_DIR:/out"
    )

    # Pass proxy settings
    [[ -n "${http_proxy:-}" ]] && docker_build_args+=(-e "http_proxy=$http_proxy")
    [[ -n "${https_proxy:-}" ]] && docker_build_args+=(-e "https_proxy=$https_proxy")
    [[ -n "${HTTP_PROXY:-}" ]] && docker_build_args+=(-e "HTTP_PROXY=$HTTP_PROXY")
    [[ -n "${HTTPS_PROXY:-}" ]] && docker_build_args+=(-e "HTTPS_PROXY=$HTTPS_PROXY")
    [[ -n "${GITHUB_TOKEN:-}" ]] && docker_build_args+=(-e "GITHUB_TOKEN=$GITHUB_TOKEN")

    # Host CA bundle for TLS-inspecting proxies
    if [[ -f /etc/ssl/certs/ca-certificates.crt ]]; then
      docker_build_args+=(-v "/etc/ssl/certs/ca-certificates.crt:/tmp/host-ca-bundle.crt:ro")
    fi

    docker run "${docker_build_args[@]}" nixos/nix:latest bash -c '
      set -euo pipefail
      mkdir -p /etc/nix
      echo "experimental-features = nix-command flakes" >> /etc/nix/nix.conf
      echo "sandbox = false" >> /etc/nix/nix.conf

      if [ -n "${GITHUB_TOKEN:-}" ]; then
        echo "access-tokens = github.com=$GITHUB_TOKEN" >> /etc/nix/nix.conf
      fi

      if [ -f /tmp/host-ca-bundle.crt ]; then
        export NIX_SSL_CERT_FILE=/tmp/host-ca-bundle.crt
      fi

      nix-build /src/image.nix -o /tmp/result
      # Copy the actual file -- the nix store symlink would not survive container exit
      cp -L /tmp/result /out/nixos-image.tar.gz
    '
  fi

  echo "Loading image into Docker..."
  if [[ -f "$SCRIPT_DIR/nixos-image.tar.gz" ]]; then
    docker load <"$SCRIPT_DIR/nixos-image.tar.gz"
  else
    docker load <"$SCRIPT_DIR/result"
  fi
  echo "=== Image $IMAGE_NAME built and loaded ==="
}

run_container() {
  local -a docker_args=(
    --rm
    --network=host
    -v "$REPO_ROOT:/repo"
    -w /repo
  )

  # Mount BuildBuddy credentials if available
  local bb_rc="${HOME}/.config/bazel/buildbuddy.bazelrc"
  if [[ -f "$bb_rc" ]]; then
    docker_args+=(-v "$bb_rc:/root/.config/bazel/buildbuddy.bazelrc:ro")
    echo "BuildBuddy credentials mounted."
  fi

  if [[ "${1:-}" == "test" ]]; then
    echo "=== Running Bazel build+test ==="
    docker run "${docker_args[@]}" "$IMAGE_NAME:latest" \
      bash -c '
        set -euo pipefail
        echo "=== NixOS filesystem checks ==="
        echo "/bin/bash exists: $(test -e /bin/bash && echo YES || echo NO)"
        echo "/usr/bin/ar exists: $(test -e /usr/bin/ar && echo YES || echo NO)"
        echo "/usr/bin/ld.gold exists: $(test -e /usr/bin/ld.gold && echo YES || echo NO)"
        echo "Bash location: $(which bash)"
        echo "/run/current-system/sw/bin/bash: $(test -e /run/current-system/sw/bin/bash && echo YES || echo NO)"
        echo "NIX_LD=${NIX_LD:-unset}"
        echo "NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-unset}"
        echo ""

        echo "=== Python (agent_core) ==="
        bazel build //agent_core/...
        bazel test //agent_core/...

        echo "=== Go (kubespan_agent) ==="
        bazel build //cluster/kubespan_agent/...

        echo "=== Rust (worthy) ==="
        bazel build //finance/worthy/...
        bazel test //finance/worthy/...

        echo "=== All languages verified ==="
      '
  else
    echo "=== Starting interactive NixOS shell ==="
    echo "This is a real NixOS system (boot.isContainer)."
    echo "Try: bazel build //agent_core/..."
    docker run -it "${docker_args[@]}" "$IMAGE_NAME:latest" bash
  fi
}

case "${1:-shell}" in
  build)
    build_image
    ;;
  test)
    build_image
    run_container test
    ;;
  shell | "")
    build_image
    run_container
    ;;
  *)
    echo "Usage: $0 [build|test|shell]"
    exit 1
    ;;
esac
