# BuildBuddy RBE worker image: the execution platform for every remote action.
#
# Its digest is an exec property of //:rbe_linux_x64, so it is part of every
# action's cache key AND of the key for BuildBuddy's Firecracker snapshot pool.
# Moving it orphans the remote action cache and dumps every warm snapshot: a
# measured repin costs the next hour of PR CI ~12x the executed actions for the
# same affected-target set. So this image carries only what Bazel cannot supply
# from the repo it is building -- the OS userland an action executes inside:
# glibc, apt system libraries, the Docker daemon, /bin/bash.
#
# Anything Bazel already provides through a toolchain or runfiles does NOT
# belong here, and neither does developer or agent tooling. Those live in
# devinfra/bbr_runner/Dockerfile, whose digest is in no action's cache key.
#
# Based on BuildBuddy's rbe-ubuntu24-04 image which provides: build-essential,
# python3, git, curl, file, zip, Docker CE, iptables-legacy (required for
# Firecracker init-dockerd -- the guest kernel lacks nftables support), and a
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
        # git-lfs: needed by the runner, which inherits this layer — bb remote
        # re-fetches the repo there via plain git fetch (no LFS smudge), so
        # consumer scripts call `git lfs pull` before bazel runs to materialize
        # LFS-tracked test data. Kept here so there is one apt list, not two.
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
    #
    # Resolved from the pool index rather than pinned to a filename. Ubuntu
    # rotates superseded point releases out of pool/, so a pinned name 404s the
    # first time this layer is not a cache hit -- and with the buildx registry
    # cache that is long after the rot, on whatever unrelated change finally
    # invalidates the layer. The previous `6.3-2ubuntu0.1` pin died exactly that
    # way: every rebuild since was triggered by a later layer, so the fetch never
    # re-ran and the breakage stayed invisible. Prefer an `ubuntu` revision (a
    # security update of the Debian version) and take the highest.
    && LIBTINFO5_POOL=http://security.ubuntu.com/ubuntu/pool/universe/n/ncurses/ \
    && LIBTINFO5_DEBS="$(curl -fsSL "$LIBTINFO5_POOL" | grep -o 'libtinfo5_[^"]*_amd64\.deb' | sort -u)" \
    && LIBTINFO5_DEB="$(printf '%s\n' "$LIBTINFO5_DEBS" | grep ubuntu | sort -V | tail -1)" \
    && [ -n "$LIBTINFO5_DEB" ] || LIBTINFO5_DEB="$(printf '%s\n' "$LIBTINFO5_DEBS" | sort -V | tail -1)" \
    && [ -n "$LIBTINFO5_DEB" ] \
    && curl -fsSL -o /tmp/libtinfo5.deb "${LIBTINFO5_POOL}${LIBTINFO5_DEB}" \
    && dpkg -i /tmp/libtinfo5.deb \
    && rm /tmp/libtinfo5.deb

# dockerd wrapper: Firecracker's guest kernel lacks CONFIG_IP_NF_RAW, so
# Docker 28's raw table rules for published ports (-p) fail. The wrapper sets
# DOCKER_INSECURE_NO_IPTABLES_RAW=1 to skip raw table ops. goinit overwrites
# daemon.json, so a wrapper is the only injection point.
# See: devinfra/rbe_image/docs/firecracker_docker_init_timeout.md
COPY devinfra/rbe_image/dockerd_wrapper.sh /usr/bin/dockerd_wrapper.sh
RUN mv /usr/bin/dockerd /usr/bin/dockerd.real \
    && mv /usr/bin/dockerd_wrapper.sh /usr/bin/dockerd \
    && chmod +x /usr/bin/dockerd
