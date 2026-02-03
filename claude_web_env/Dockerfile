# Claude Code Web Container Recreation
# See docs/container-spec.md for runtime details and gVisor constraints.
#
# Build (gVisor/Claude Code web — see AGENTS.md for tmpfs setup):
#   CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
#     podman build --layers=false --network=host --isolation=oci \
#     --format=docker -t claude-code-web-recreated .
#
# See README.md for the full build/capture/diff recipe.

FROM docker.io/library/ubuntu:24.04

LABEL maintainer="Claude Code Recreation"
LABEL description="Recreated Claude Code web development environment"

# Redirect RUN output to a log file to avoid SIGPIPE under gVisor+buildah.
# Requires --format=docker on podman build (SHELL is a Docker extension).
SHELL ["/bin/bash", "-c", "set -euo pipefail; exec > /tmp/build-step.log 2>&1; eval \"$0\""]

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ============================================================================
# APT SNAPSHOT REPOS (pinned to Dec 1 2025 for exact version reproducibility)
# ============================================================================
# Replace default ubuntu.sources with snapshot-only HTTPS sources.
# The base image ships packages newer than the Dec 1 snapshot, so we:
#   1. Replace sources with snapshot-only URLs
#   2. Pin snapshot origin to priority 1001 (forces downgrades)
#   3. Disable TLS verification initially (proxy CA not yet trusted)
#   4. Downgrade conflicting base image packages + install ca-certificates
#   5. Install proxy CAs, re-enable TLS verification
#   6. All subsequent installs use exact Dec 1 2025 package versions

# SWP / proxy CA certs (needed for TLS-inspecting proxy)
COPY rootfs/usr/local/share/ca-certificates/swp-ca-production.crt /usr/local/share/ca-certificates/swp-ca-production.crt
COPY rootfs/usr/local/share/ca-certificates/swp-ca-staging.crt /usr/local/share/ca-certificates/swp-ca-staging.crt

# Replace default sources with snapshot-only (Dec 1 2025)
RUN rm -f /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list && \
    printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/20251201T000000Z\nSuites: noble noble-updates noble-backports noble-security noble-proposed\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/snapshot.sources && \
    printf '%s\n' 'Package: *' 'Pin: origin snapshot.ubuntu.com' 'Pin-Priority: 1001' \
      > /etc/apt/preferences.d/snapshot-pin && \
    printf '%s\n' 'Package: libglib2.0-* gir1.2-glib-2.0 libgirepository-2.0-0' \
      'Pin: version 2.80.0-6ubuntu3.5' 'Pin-Priority: 1002' \
      > /etc/apt/preferences.d/glib-pin && \
    printf '%s\n' \
      'Package: libreoffice-* fonts-opensymbol libuno-* uno-libs-private ure python3-uno libreoffice-style-* libreoffice-uiconfig-*' \
      'Pin: version 4:24.2*' \
      'Pin-Priority: 1003' \
      > /etc/apt/preferences.d/libreoffice-pin && \
    printf '%s\n' \
      'Package: postgresql-* libpq5 libpq-dev' \
      'Pin: version 16.11*' \
      'Pin-Priority: 1003' \
      > /etc/apt/preferences.d/postgresql-pin && \
    printf 'Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble-updates noble-security\nComponents: main\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/live-archive.sources && \
    printf '%s\n' 'Package: *' 'Pin: origin archive.ubuntu.com' 'Pin-Priority: 100' \
      > /etc/apt/preferences.d/live-archive-pin && \
    echo 'Acquire::https::Verify-Peer "false";' > /etc/apt/apt.conf.d/99no-verify

# Downgrade base image packages to snapshot versions, install ca-certificates,
# then install proxy CAs and re-enable TLS verification.
RUN apt-get update && \
    apt-get install --allow-downgrades -y gpgv libsystemd0 libudev1 && \
    apt-get install --allow-downgrades -y --no-install-recommends ca-certificates && \
    update-ca-certificates && \
    rm /etc/apt/apt.conf.d/99no-verify && \
    rm -rf /var/lib/apt/lists/*

# Node.js, Rust, Go, and Python tools use these env vars for TLS trust.
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# ============================================================================
# APT PACKAGES (from snapshot repos)
# ============================================================================

# Add PHP repository (for PHP 8.4) using deb822 .sources format matching live container
COPY rootfs/etc/apt/sources.list.d/ondrej-ubuntu-php-noble.sources /etc/apt/sources.list.d/ondrej-ubuntu-php-noble.sources
RUN apt-get update && apt-get install --allow-downgrades -y --no-install-recommends \
    software-properties-common gnupg2 curl && \
    printf '%s\n' 'Package: *' 'Pin: origin ppa.launchpadcontent.net' 'Pin-Priority: 1002' \
      > /etc/apt/preferences.d/ppa-pin

# Install core packages (versions frozen by snapshot repos — no individual pins needed)
RUN apt-get update && apt-get install --allow-downgrades -y --no-install-recommends \
    # Base system
    adduser apt-transport-https base-files bash bc \
    bsdutils bzip2 \
    ca-certificates coreutils curl dash diffutils dpkg file findutils \
    gzip hostname less locales login lsof m4 mawk \
    mount nano netbase \
    netcat-openbsd passwd patch perl procps psmisc sed sensible-utils \
    sudo tar tmux tzdata unzip wget xz-utils zip \
    util-linux \
    # Build essentials
    autoconf automake autotools-dev binutils bison build-essential \
    cmake cmake-data cpp cpp-13 dpkg-dev g++ g++-13 gcc gcc-13 \
    gdb libc6-dev libtool lto-disabled-list m4 make ninja-build patch pkg-config \
    # Clang/LLVM toolchain
    clang clang-18 clang-format clang-format-18 clang-tidy clang-tidy-18 \
    clang-tools-18 lld lld-18 lldb lldb-18 llvm llvm-18 llvm-18-runtime \
    # Libraries and headers
    libssl-dev libssl3t64 openssl \
    libsqlite3-dev libreadline-dev libncurses-dev \
    libbz2-dev libffi-dev zlib1g-dev \
    libyaml-dev libgdbm-dev libgdbm-compat-dev libdb-dev uuid-dev \
    liblzma-dev \
    libpng-dev libpng16-16t64 \
    libbrotli-dev \
    libblkid-dev libblkid1 \
    libmount-dev libmount1 \
    libselinux1-dev libsepol-dev \
    libcairo2-dev libpixman-1-dev libxcb-render0-dev libxcb-shm0-dev \
    libxrender-dev libxext-dev libfontconfig-dev libfreetype-dev \
    libglib2.0-dev libglib2.0-dev-bin \
    libglib2.0-0t64 libglib2.0-bin libglib2.0-data \
    x11proto-core-dev \
    # Development tools
    git \
    gnupg gnupg2 gnupg-utils \
    gpg gpg-agent gpgconf gpgsm gpgv dirmngr keyboxd \
    jq ripgrep strace valgrind vim xvfb yq \
    # Systemd
    systemd systemd-dev systemd-sysv \
    libsystemd0 libsystemd-shared libudev1 libpam-systemd \
    # Container runtime (podman stack)
    podman buildah crun conmon catatonit fuse-overlayfs fuse3 \
    slirp4netns passt netavark aardvark-dns uidmap \
    containernetworking-plugins \
    # Fonts
    fonts-dejavu-core fonts-dejavu-mono \
    fonts-freefont-ttf fonts-ipafont-gothic fonts-liberation \
    fonts-noto-color-emoji fonts-opensymbol fonts-tlwg-loma-otf \
    fonts-unifont fonts-wqy-zenhei \
    xfonts-cyrillic xfonts-scalable \
    # LibreOffice (core + Python bindings)
    libreoffice-core libreoffice-common python3-uno \
    # JavaScript documentation libraries
    libjs-jquery libjs-sphinxdoc libjs-underscore \
    # Python documentation
    python3-doc \
    # Firewall
    nftables \
    # Python 3.12 dev headers (provides python3-config, pkg-config files)
    libpython3-dev \
    # Java
    openjdk-21-jdk openjdk-21-jre ca-certificates-java \
    # Database servers and clients
    postgresql-16 postgresql-common postgresql-client-16 \
    redis-server redis-tools \
    # Missing libs (found via manifest diff)
    libwayland-server0 \
    libdrm2 libdrm-amdgpu1 libdrm-intel1 libdrm-common \
    # XML/XSLT tools
    libxml2 libxml2-utils \
    # Dependency-only packages (pulled for exact binary match)
    libfdisk1 libsmartcols1 libuuid1 \
    libxslt1.1 libsodium23 libtasn1-6 \
    libavahi-client3 libavahi-common3 libavahi-common-data \
    libheif1 libheif-plugin-aomdec libheif-plugin-libde265 \
    libgirepository-2.0-0 \
    mesa-libgallium libgbm1 libgl1-mesa-dri libglx-mesa0 \
    linux-libc-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python versions from deadsnakes PPA
# Use deb822 .sources format matching live container (GPG key inline).
COPY rootfs/etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-noble.sources /etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-noble.sources
RUN apt-get update && apt-get install --allow-downgrades -y --no-install-recommends \
    python3.10 python3.10-dev python3.10-venv python3.10-distutils \
    python3.11 python3.11-dev python3.11-venv python3.11-distutils \
    python3.12 python3.12-dev python3.12-venv python3.12-minimal \
    libpython3.12-minimal libpython3.12-stdlib \
    libpython3.12-dev libpython3.12t64 \
    python3.13 python3.13-dev python3.13-venv \
    python3-pip python3-setuptools python3-wheel \
    # Pin python3-apt to match live container version
    python3-apt=2.7.7ubuntu5.1 python-apt-common=2.7.7ubuntu5.1 \
    && rm -rf /var/lib/apt/lists/*

# Install PHP 8.4 with specific extensions matching live container
# Pin to exact version for binary reproducibility
COPY rootfs/etc/apt/preferences.d/php84-pin /etc/apt/preferences.d/php84-pin
RUN apt-get update && apt-get install --allow-downgrades -y --no-install-recommends \
    php-common \
    php8.4-cli php8.4-common php8.4-curl php8.4-dev php8.4-gd \
    php8.4-intl php8.4-mbstring php8.4-mysql php8.4-opcache \
    php8.4-pgsql php8.4-readline php8.4-xml php8.4-zip \
    php8.4-igbinary php8.4-redis \
    && rm -rf /var/lib/apt/lists/*

# ============================================================================
# NODE.JS INSTALLATIONS
# ============================================================================

# Install Node.js 20, 21, 22
RUN mkdir -p /opt && \
    curl -fsSL https://nodejs.org/dist/v20.19.6/node-v20.19.6-linux-x64.tar.xz | \
        tar -xJ -C /opt && mv /opt/node-v20.19.6-linux-x64 /opt/node20 && \
    curl -fsSL https://nodejs.org/dist/v21.7.3/node-v21.7.3-linux-x64.tar.xz | \
        tar -xJ -C /opt && mv /opt/node-v21.7.3-linux-x64 /opt/node21 && \
    curl -fsSL https://nodejs.org/dist/v22.21.1/node-v22.21.1-linux-x64.tar.xz | \
        tar -xJ -C /opt && mv /opt/node-v22.21.1-linux-x64 /opt/node22

# Set Node 22 as default
ENV PATH="/opt/node22/bin:$PATH"

# Install NVM (tarball to avoid git CONNECT issues behind proxies)
RUN mkdir -p /opt/nvm && \
    curl -fsSL https://github.com/nvm-sh/nvm/archive/refs/tags/v0.40.1.tar.gz | \
    tar -xz --strip-components=1 -C /opt/nvm

# Install npm global packages
# chromedriver and playwright have post-install scripts that download binaries
# from external hosts (storage.googleapis.com). Use --ignore-scripts for those,
# then install the rest normally.
RUN npm install -g --ignore-scripts chromedriver@143.0.0 playwright@1.56.1 && \
    npm install -g \
    @anthropic-ai/claude-code@2.0.59 \
    eslint@9.39.1 \
    http-server@14.1.1 \
    nodemon@3.1.11 \
    pnpm@10.25.0 \
    prettier@3.7.4 \
    serve@14.2.5 \
    ts-node@10.9.2 \
    typescript@5.9.3 \
    yarn@1.22.22

# ============================================================================
# RUBY INSTALLATIONS
# ============================================================================

# Install rbenv (for shims) and ruby-build plugin
RUN mkdir -p /opt/rbenv /opt/rbenv/plugins/ruby-build && \
    curl -fsSL https://github.com/rbenv/rbenv/archive/refs/heads/master.tar.gz | \
    tar -xz --strip-components=1 -C /opt/rbenv && \
    curl -fsSL https://github.com/rbenv/ruby-build/archive/refs/heads/master.tar.gz | \
    tar -xz --strip-components=1 -C /opt/rbenv/plugins/ruby-build

ENV RBENV_ROOT=/opt/rbenv
ENV PATH="$RBENV_ROOT/bin:$RBENV_ROOT/shims:$PATH"

# Install pre-built Ruby binaries from ruby/ruby-builder (GitHub Actions toolcache).
# Building from source is nondeterministic and slow; pre-built binaries are faster
# and more reliable. The tarballs extract to x64/ which we move to rbenv's versions dir.
RUN for ver in 3.1.6 3.2.6 3.3.6; do \
        mkdir -p /opt/rbenv/versions/$ver && \
        curl -fsSL "https://github.com/ruby/ruby-builder/releases/download/toolcache/ruby-${ver}-ubuntu-24.04.tar.gz" | \
        tar -xz --strip-components=1 -C /opt/rbenv/versions/$ver; \
    done && \
    eval "$(rbenv init -)" && rbenv global 3.3.6 && rbenv rehash

# Create /opt/ruby-X.X.X symlinks for compatibility
RUN ln -s /opt/rbenv/versions/3.1.6 /opt/ruby-3.1.6 && \
    ln -s /opt/rbenv/versions/3.2.6 /opt/ruby-3.2.6 && \
    ln -s /opt/rbenv/versions/3.3.6 /opt/ruby-3.3.6

# ============================================================================
# GO INSTALLATION
# ============================================================================

# Install Go to versioned directories (matching live container layout)
RUN curl -fsSL https://go.dev/dl/go1.24.7.linux-amd64.tar.gz | tar -xz -C /usr/local && \
    mv /usr/local/go /usr/local/go1.24.7 && \
    curl -fsSL https://go.dev/dl/go1.25.1.linux-amd64.tar.gz | tar -xz -C /usr/local && \
    mv /usr/local/go /usr/local/go1.25.1

# Default to Go 1.25.1
ENV PATH="$PATH:/usr/local/go1.25.1/bin"
ENV GOPATH="/root/go"

# Version-switching scripts
RUN printf '#!/bin/bash\nexport PATH="/usr/local/go1.24.7/bin:${PATH//\/usr\/local\/go[0-9]*/}"\necho "Switched to Go $(go version)"\n' > /usr/local/use-go-1.24.7.sh && \
    printf '#!/bin/bash\nexport PATH="/usr/local/go1.25.1/bin:${PATH//\/usr\/local\/go[0-9]*/}"\necho "Switched to Go $(go version)"\n' > /usr/local/use-go-1.25.1.sh && \
    chmod +x /usr/local/use-go-1.24.7.sh /usr/local/use-go-1.25.1.sh

# Install golangci-lint
RUN curl -fsSL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh -o /tmp/golangci-lint-install.sh && \
    sh /tmp/golangci-lint-install.sh -b /usr/local/bin v1.61.0 && \
    rm /tmp/golangci-lint-install.sh

# ============================================================================
# RUST INSTALLATION
# ============================================================================

RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/rustup-init.sh && \
    sh /tmp/rustup-init.sh -y --default-toolchain stable --profile minimal && \
    rm /tmp/rustup-init.sh
ENV PATH="/root/.cargo/bin:$PATH"

# ============================================================================
# BUN INSTALLATION
# ============================================================================

RUN curl -fsSL https://bun.sh/install -o /tmp/bun-install.sh && \
    bash /tmp/bun-install.sh "bun-v1.3.4" && \
    rm /tmp/bun-install.sh
ENV BUN_INSTALL="/root/.bun"
ENV PATH="$BUN_INSTALL/bin:$PATH"

# ============================================================================
# UV (PYTHON PACKAGE MANAGER)
# ============================================================================

RUN curl -fsSL https://astral.sh/uv/0.8.17/install.sh -o /tmp/uv-install.sh && \
    sh /tmp/uv-install.sh && \
    rm /tmp/uv-install.sh
ENV PATH="/root/.local/bin:$PATH"

# Install UV tools (use system Python to avoid downloading cpython in build)
ENV UV_PYTHON_PREFERENCE=only-system
RUN /root/.local/bin/uv tool install black && \
    /root/.local/bin/uv tool install flake8 && \
    /root/.local/bin/uv tool install mypy && \
    /root/.local/bin/uv tool install poetry && \
    /root/.local/bin/uv tool install pyright && \
    /root/.local/bin/uv tool install pytest && \
    /root/.local/bin/uv tool install ruff

# ============================================================================
# MAVEN AND GRADLE
# ============================================================================

# Maven 3.9.11
RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.tar.gz | \
    tar -xz -C /opt && \
    ln -s /opt/apache-maven-3.9.11 /opt/maven

ENV MAVEN_HOME=/opt/maven
ENV PATH="$MAVEN_HOME/bin:$PATH"

# Gradle 8.14.3
RUN curl -fsSL "https://services.gradle.org/distributions/gradle-8.14.3-bin.zip" -o /tmp/gradle.zip && \
    unzip -q /tmp/gradle.zip -d /opt && \
    ln -s /opt/gradle-8.14.3 /opt/gradle && \
    rm /tmp/gradle.zip

ENV GRADLE_HOME=/opt/gradle
ENV PATH="$GRADLE_HOME/bin:$PATH"

# ============================================================================
# PHP COMPOSER
# ============================================================================

RUN curl -fsSL https://getcomposer.org/installer -o /tmp/composer-setup.php && \
    php /tmp/composer-setup.php --install-dir=/usr/local/bin --filename=composer && \
    rm /tmp/composer-setup.php

# ============================================================================
# PYTHON PIP PACKAGES
# ============================================================================

RUN pip3 install --break-system-packages \
    anyio==4.12.1 \
    argcomplete==3.1.4 \
    certifi==2025.11.12 \
    charset-normalizer==3.4.4 \
    conan==2.23.0 \
    colorama==0.4.6 \
    h11==0.16.0 \
    h2==4.3.0 \
    httpcore==1.0.9 \
    httpx==0.28.1 \
    idna==3.11 \
    Jinja2==3.1.6 \
    MarkupSafe==3.0.3 \
    packaging==24.0 \
    patch-ng==1.18.1 \
    pre-commit \
    PyYAML==6.0.1 \
    requests==2.32.5 \
    toml==0.10.2 \
    typing_extensions==4.15.0 \
    urllib3==2.6.1 \
    virtualenv \
    websockets==16.0 \
    xmltodict==0.13.0 \
    yq==3.1.0

# ============================================================================
# SYMLINKS AND SCRIPTS
# ============================================================================

# Create symlinks in /usr/local/bin
RUN ln -sf /root/.bun/bin/bun /usr/local/bin/bun && \
    ln -sf /root/.bun/bin/bunx /usr/local/bin/bunx && \
    ln -sf /opt/ruby-3.3.6/bin/bundle /usr/local/bin/bundle && \
    ln -sf /opt/ruby-3.3.6/bin/gem /usr/local/bin/gem && \
    ln -sf /opt/ruby-3.3.6/bin/irb /usr/local/bin/irb && \
    ln -sf /opt/ruby-3.3.6/bin/ruby /usr/local/bin/ruby && \
    ln -sf /opt/node20/bin/node /usr/local/bin/node && \
    ln -sf /opt/node20/bin/npm /usr/local/bin/npm && \
    ln -sf /opt/node20/bin/npx /usr/local/bin/npx && \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python && \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python3

# ============================================================================
# PROFILE SCRIPTS
# ============================================================================

COPY rootfs/etc/profile.d/nodejs.sh /etc/profile.d/nodejs.sh
COPY rootfs/etc/profile.d/java.sh /etc/profile.d/java.sh
COPY rootfs/etc/profile.d/gradle.sh /etc/profile.d/gradle.sh
COPY rootfs/etc/profile.d/maven.sh /etc/profile.d/maven.sh
COPY rootfs/etc/profile.d/ruby.sh /etc/profile.d/ruby.sh
COPY rootfs/etc/profile.d/rbenv.sh /etc/profile.d/rbenv.sh
COPY rootfs/etc/profile.d/bun.sh /etc/profile.d/bun.sh
COPY rootfs/etc/profile.d/nvm.sh /etc/profile.d/nvm.sh

# ============================================================================
# HELPER SCRIPTS
# ============================================================================

# use-python
COPY rootfs/usr/local/bin/use-python /usr/local/bin/use-python
RUN chmod +x /usr/local/bin/use-python

# Node version-switching scripts
COPY rootfs/usr/local/bin/use-node-20 /usr/local/bin/use-node-20
COPY rootfs/usr/local/bin/use-node-21 /usr/local/bin/use-node-21
COPY rootfs/usr/local/bin/use-node-22 /usr/local/bin/use-node-22
RUN chmod +x /usr/local/bin/use-node-20 /usr/local/bin/use-node-21 /usr/local/bin/use-node-22

# Ruby version-switching scripts
COPY rootfs/usr/local/bin/use-ruby-3.1.6 /usr/local/bin/use-ruby-3.1.6
COPY rootfs/usr/local/bin/use-ruby-3.2.6 /usr/local/bin/use-ruby-3.2.6
COPY rootfs/usr/local/bin/use-ruby-3.3.6 /usr/local/bin/use-ruby-3.3.6
RUN chmod +x /usr/local/bin/use-ruby-3.1.6 /usr/local/bin/use-ruby-3.2.6 /usr/local/bin/use-ruby-3.3.6

# Python venv creation scripts
COPY rootfs/usr/local/bin/create-venv-py3.10 /usr/local/bin/create-venv-py3.10
COPY rootfs/usr/local/bin/create-venv-py3.11 /usr/local/bin/create-venv-py3.11
COPY rootfs/usr/local/bin/create-venv-py3.12 /usr/local/bin/create-venv-py3.12
COPY rootfs/usr/local/bin/create-venv-py3.13 /usr/local/bin/create-venv-py3.13
RUN chmod +x /usr/local/bin/create-venv-py3.10 /usr/local/bin/create-venv-py3.11 \
    /usr/local/bin/create-venv-py3.12 /usr/local/bin/create-venv-py3.13

# ============================================================================
# LIVE CONTAINER BINARIES AND SCRIPTS
# ============================================================================
# Snapshotted from live container — these are part of the Claude Code runtime
# Large binaries stored compressed in reference/*.gz to reduce git repo size
COPY reference/environment-manager.gz /tmp/environment-manager.gz
RUN gunzip -c /tmp/environment-manager.gz > /usr/local/bin/environment-manager && \
    chmod +x /usr/local/bin/environment-manager && rm /tmp/environment-manager.gz
COPY rootfs/usr/local/bin/check-tools /usr/local/bin/check-tools
RUN chmod +x /usr/local/bin/check-tools
COPY rootfs/usr/local/bin/use-php-8.4 /usr/local/bin/use-php-8.4
RUN chmod +x /usr/local/bin/use-php-8.4

# ============================================================================
# PROCESS API SERVER
# ============================================================================
# Proprietary server from live container (stored compressed as reference/process_api.gz)
COPY reference/process_api.gz /tmp/process_api.gz
RUN mkdir -p /process_api && \
    gunzip -c /tmp/process_api.gz > /process_api/process_api && \
    chmod +x /process_api/process_api && rm /tmp/process_api.gz

# ============================================================================
# USER SETUP
# ============================================================================

# Create claude user (ubuntu:1000 already exists in base image)
RUN useradd -m -s /bin/bash -u 999 claude

# Create directory structure
RUN mkdir -p /home/user /home/claude/.claude /home/claude/.ssh /home/claude/scripts

# /root/.local/bin/env
COPY rootfs/root/.local/bin/env /root/.local/bin/env
RUN chmod +x /root/.local/bin/env

# Root shell dotfiles (captured from live container)
COPY rootfs/root/.bashrc /root/.bashrc
COPY rootfs/root/.profile /root/.profile
COPY rootfs/root/.wget-hsts /root/.wget-hsts

# Git config
COPY rootfs/root/.gitconfig /root/.gitconfig
RUN cp /root/.gitconfig /home/claude/.gitconfig && \
    chown claude:claude /home/claude/.gitconfig

# Claude home README
COPY rootfs/home/claude/README.md /home/claude/README.md

# Claude scripts directory README
COPY rootfs/home/claude/scripts/README /home/claude/scripts/README

# ============================================================================
# CLAUDE CODE CONFIGURATION
# ============================================================================

# Claude Code settings and hooks for both root and claude user
COPY rootfs/root/.claude/settings.json /root/.claude/settings.json
COPY rootfs/root/.claude/skills/session-start-hook/SKILL.md /root/.claude/skills/session-start-hook/SKILL.md

COPY rootfs/home/claude/.claude/settings.json /home/claude/.claude/settings.json
COPY rootfs/home/claude/.claude/skills/session-start-hook/SKILL.md /home/claude/.claude/skills/session-start-hook/SKILL.md
COPY rootfs/home/claude/.claude/stop-hook-git-check.sh /home/claude/.claude/stop-hook-git-check.sh
RUN chmod +x /home/claude/.claude/stop-hook-git-check.sh && \
    chown -R claude:claude /home/claude/.claude

# ============================================================================
# PERMISSIONS AND SYMLINKS
# ============================================================================

# Fix SUID/SGID bits to match live container.
# These are dropped by buildah/podman during image build and need explicit restoration.
RUN chmod 4755 /usr/bin/chfn /usr/bin/chsh /usr/bin/gpasswd /usr/bin/newgrp \
               /usr/bin/passwd /usr/bin/sudo \
               /usr/lib/dbus-1.0/dbus-daemon-launch-helper \
               /usr/lib/polkit-1/polkit-agent-helper-1 && \
    chmod 2755 /usr/bin/chage /usr/bin/expiry \
               /usr/sbin/pam_extrausers_chkpwd /usr/sbin/unix_chkpwd && \
    (chmod 4755 /usr/lib/x86_64-linux-gnu/gstreamer1.0/gstreamer-1.0/gst-ptp-helper 2>/dev/null || true) && \
    (chmod 2755 /usr/lib/x86_64-linux-gnu/utempter/utempter 2>/dev/null || true)

# Fix directory permissions to match live container
RUN chmod 2770 /etc/redis && \
    chmod 1733 /var/lib/php/sessions && \
    chmod 2775 /var/local /var/mail /usr/local/share/fonts && \
    chmod 555 /

# Fix /etc/mtab symlink (live uses absolute path)
RUN ln -sf /proc/mounts /etc/mtab

# Fix gcc/g++/python3 symlinks to use update-alternatives (matching live)
RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 130 && \
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-13 130 && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

# Create directories that exist in live (populated at runtime)
RUN mkdir -p /root/.local/share/gem /root/.local/lib/python3.11

# ============================================================================
# STRIP UNUSED FILES (match live container size optimization)
# ============================================================================

# The live container strips documentation, headers, man pages, and sbin to reduce
# image size. We replicate this to match the live filesystem exactly.
RUN rm -rf \
    /usr/share/doc \
    /usr/share/doc-base \
    /usr/share/man \
    /usr/share/X11 \
    /usr/include \
    /usr/src \
    /usr/sbin \
    /usr/share/PackageKit \
    /usr/local/include \
    /usr/local/sbin \
    /usr/local/src \
    /etc/apt/preferences.d \
    /etc/apt/sources.list.d/snapshot.sources \
    /etc/apt/sources.list.d/live-archive.sources

# ============================================================================
# ENVIRONMENT
# ============================================================================

ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/user

CMD ["/bin/bash"]
