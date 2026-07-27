# BuildBuddy RBE worker image with extra tooling for this repo.
#
# Based on BuildBuddy's rbe-ubuntu24-04 image which provides: build-essential,
# python3, git, curl, file, zip, Docker CE, iptables-legacy (required for
# Firecracker init-dockerd — the guest kernel lacks nftables support), and a
# non-root "buildbuddy" user with passwordless sudo.
#
# We layer on: libssl-dev + pkg-config (Rust toolchain), libtinfo5 (GHC
# toolchain), and Chromium shared libraries (headless_shell from
# rules_playwright for visual regression tests).
#
# The Nix equivalents of these system libraries are in
# nix/packages/system-libs.nix (shared by devShell and nix RBE image).
# Keep the two lists in sync when adding/removing libraries.
FROM gcr.io/flame-public/rbe-ubuntu24-04:latest
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

# Enable universe repo and install additional dependencies.
RUN sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libssl-dev \
        pkg-config \
        # clang: needed by pip to build Python packages with C extensions
        # (e.g., lru-dict) during rules_python whl_library fetch
        clang \
        # dbus-daemon: needed by tests that spawn a private D-Bus session
        # (e.g. experimental/dbus_fast_example)
        dbus-daemon \
        # git-lfs: bb remote re-fetches the repo on the runner via plain
        # git fetch (no LFS smudge), so consumer scripts call `git lfs pull`
        # before bazel runs to materialize LFS-tracked test data.
        git-lfs \
        # Native dev headers for pip wheel builds (pygobject, pycairo, dbus-python)
        libgirepository-2.0-dev \
        libcairo2-dev \
        libdbus-1-dev \
        # xvfb: virtual framebuffer for headless GUI tests (e.g. FreeCAD 3D rendering with OpenGL)
        xvfb \
        # Chromium headless shell shared library dependencies
        libasound2t64 \
        libatk-bridge2.0-0t64 \
        libatk1.0-0t64 \
        libcups2t64 \
        libdrm2 \
        libgbm1 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxkbcommon0 \
        libxrandr2 \
        libxfixes3 \
        libxshmfence1 \
    && rm -rf /var/lib/apt/lists/* \
    # libtinfo5: dropped from noble (24.04), fetch from jammy (22.04) archive.
    # GHC 9.0.2 bindist requires libtinfo.so.5 for ghc-pkg during toolchain setup.
    && curl -fsSL -o /tmp/libtinfo5.deb \
        http://security.ubuntu.com/ubuntu/pool/universe/n/ncurses/libtinfo5_6.3-2ubuntu0.1_amd64.deb \
    && dpkg -i /tmp/libtinfo5.deb \
    && rm /tmp/libtinfo5.deb

# --- Nix + devtools ---
# Install Nix (single-user, no daemon) and the flake devtools package.
# This gives the RBE image the same tools as the local devshell (pre-commit,
# bazelisk, ruff, buildifier, bb, bbapi, gh, kubectl, etc.) without
# maintaining a separate apt/pip install list.
#
# The devtools closure is fetched from cache.nixos.org (and attic when
# available). Only the symlinkJoin derivation itself needs a local build.

# Copy only the repo files needed to evaluate and build `.#rbetools`. Keep
# this list in sync with the rbe-worker path filters in
# .github/workflows/container-images.yml. Pull requests build this image
# without publishing it, so missing evaluation inputs fail before merge.
COPY flake.nix flake.lock /tmp/flake/
COPY nix/artifact-pins.json /tmp/flake/nix/artifact-pins.json
COPY nix/packages /tmp/flake/nix/packages
# nix/packages/gaffer.nix reads ../gaffer-pins.json during package-set
# evaluation, even though rbetools does not include the private artifacts.
COPY nix/gaffer-pins.json /tmp/flake/nix/gaffer-pins.json
COPY skills/skills_registry.json /tmp/flake/skills/skills_registry.json
SHELL ["/bin/bash", "-c"]
RUN mkdir -p ~/.config/nix \
    # nix.conf MUST be written BEFORE the installer runs — the installer
    # internally runs nix-env which reads this config. Without build-users-group=,
    # it defaults to 'nixbld' which doesn't exist in the container.
    && printf '%s\n' \
         'build-users-group =' \
         'experimental-features = nix-command flakes' \
         'sandbox = false' \
         'max-jobs = auto' \
       > ~/.config/nix/nix.conf \
    && curl -fsSL https://nixos.org/nix/install -o /tmp/nix-install.sh \
    && sh /tmp/nix-install.sh --no-daemon \
    && rm /tmp/nix-install.sh \
    && export PATH="$HOME/.nix-profile/bin:$PATH" \
    && cd /tmp/flake \
    # Use nix build (not nix profile install) to avoid the profile manifest
    # keeping a reference to the nixpkgs source tree (~457MB).
    && RBETOOLS=$(nix build .#rbetools \
         --no-link --print-out-paths) \
    # Register as GC root so nix-collect-garbage keeps the closure.
    && nix-store --add-root /nix/var/nix/gcroots/rbetools -r "$RBETOOLS" \
    # Symlink binaries into /usr/local/bin (goinit PATH).
    && for bin in "$RBETOOLS"/bin/*; do \
         ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"; \
       done \
    # Remove profile, flake source, and anything not in the rbetools closure.
    # Run GC before removing nix-profile (which has nix binaries on PATH).
    && nix-collect-garbage -d \
    && rm -rf ~/.nix-profile ~/.nix-defexpr /tmp/flake

# dockerd wrapper: Firecracker's guest kernel lacks CONFIG_IP_NF_RAW, so
# Docker 28's raw table rules for published ports (-p) fail. The wrapper sets
# DOCKER_INSECURE_NO_IPTABLES_RAW=1 to skip raw table ops. goinit overwrites
# daemon.json, so a wrapper is the only injection point.
# See: devinfra/rbe_image/docs/firecracker_docker_init_timeout.md
COPY devinfra/rbe_image/dockerd_wrapper.sh /usr/bin/dockerd_wrapper.sh
RUN mv /usr/bin/dockerd /usr/bin/dockerd.real \
    && mv /usr/bin/dockerd_wrapper.sh /usr/bin/dockerd \
    && chmod +x /usr/bin/dockerd
