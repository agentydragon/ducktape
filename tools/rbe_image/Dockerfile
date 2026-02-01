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
FROM gcr.io/flame-public/rbe-ubuntu24-04:latest

# Enable universe repo and install additional dependencies.
RUN sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libssl-dev \
        pkg-config \
        # dbus-daemon: needed by tests that spawn a private D-Bus session
        # (e.g. experimental/dbus_fast_example)
        dbus-daemon \
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
