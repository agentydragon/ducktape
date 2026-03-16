#!/usr/bin/env bash
# Build a real NixOS Docker image and run Bazel inside it.
#
# Uses NixOS's built-in docker-image.nix module to produce a system tarball.
# On container start, /init (systemd) runs NixOS activation — /etc, nix-ld,
# home-manager (bazelrc, direnv), etc. are all set up automatically.
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
  local tarball

  if command -v nix &>/dev/null; then
    echo "Using nix build (flake)..."
    nix build "path:$REPO_ROOT#bazel-test-docker" -o "$SCRIPT_DIR/result"
    # system.build.tarball produces result/tarball/*.tar.xz
    tarball="$(echo "$SCRIPT_DIR"/result/tarball/*.tar.xz)"
  else
    echo "No local nix found, building inside nixos/nix container..."

    local -a docker_build_args=(
      --rm
      --network=host
      -v "$REPO_ROOT:/repo:ro"
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

      nix build "path:/repo#bazel-test-docker" -o /tmp/result
      # Copy the tarball out — the nix store symlink would not survive container exit
      cp -L /tmp/result/tarball/*.tar.xz /out/nixos-system.tar.xz
    '
    tarball="$SCRIPT_DIR/nixos-system.tar.xz"
  fi

  echo "Importing image into Docker..."
  docker import "$tarball" "$IMAGE_NAME:latest"
  echo "=== Image $IMAGE_NAME imported ==="
}

run_container() {
  local -a docker_args=(
    --rm
    --network=host
    -v "$REPO_ROOT:/repo"
    -w /repo
    # systemd needs tmpfs on /run and /tmp, cgroup access
    --tmpfs /run
    --tmpfs /tmp
    -v /sys/fs/cgroup:/sys/fs/cgroup:ro
  )

  # Mount BuildBuddy credentials if available
  local bb_rc="${HOME}/.config/bazel/buildbuddy.bazelrc"
  if [[ -f "$bb_rc" ]]; then
    docker_args+=(-v "$bb_rc:/root/.config/bazel/buildbuddy.bazelrc:ro")
    echo "BuildBuddy credentials mounted."
  fi

  if [[ "${1:-}" == "test" ]]; then
    echo "=== Running Bazel build+test ==="
    # Start the NixOS container (systemd activates everything), then exec commands
    local container_id
    container_id=$(docker run -d "${docker_args[@]}" "$IMAGE_NAME:latest" /init)
    # Wait for activation to complete
    echo "Waiting for NixOS activation..."
    sleep 3

    docker exec "$container_id" bash -lc '
      set -euo pipefail
      echo "=== NixOS filesystem checks ==="
      echo "/bin/bash exists: $(test -e /bin/bash && echo YES || echo NO)"
      echo "Bash location: $(which bash)"
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
    local exit_code=$?
    docker stop "$container_id" >/dev/null 2>&1 || true
    return $exit_code
  else
    echo "=== Starting NixOS container ==="
    echo "This is a real NixOS system with systemd."
    echo "Exec into it with: docker exec -it <container> bash -l"
    echo ""
    # Start systemd, then exec into the container interactively
    local container_id
    container_id=$(docker run -d "${docker_args[@]}" "$IMAGE_NAME:latest" /init)
    echo "Waiting for NixOS activation..."
    sleep 3
    echo "Container $container_id running. Dropping into shell..."
    docker exec -it "$container_id" bash -l
    echo "Stopping container..."
    docker stop "$container_id" >/dev/null 2>&1 || true
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
