# Claude Code Web Container Recreation
# Optimized for overlay layer caching (~45 layers, within ~47-50 kernel limit)
#
# Build with overlay caching (recommended):
#   mount -t tmpfs -o size=200G,exec tmpfs /tmp/tmpfs-overlay
#   mkdir -p /tmp/tmpfs-overlay/containers/{storage,run}
#   cat > /tmp/storage-overlay.conf << 'EOF'
#   [storage]
#   driver = "overlay"
#   runroot = "/tmp/tmpfs-overlay/containers/run"
#   graphroot = "/tmp/tmpfs-overlay/containers/storage"
#   EOF
#   CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf \
#     podman build --network=host --isolation=oci --format=docker \
#     -t claude-code-web-recreated .
#
# Build without caching (VFS, slower but no layer limit):
#   CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
#     podman build --layers=false --network=host --isolation=oci \
#     --format=docker -t claude-code-web-recreated .
#
# Prerequisites:
#   Proprietary binaries must be captured first (run from within live container):
#     ./tools/build_and_diff.sh --capture-binaries
#   This writes reference/environment-manager.gz and reference/process_api.gz
#   which the Dockerfile COPYs and decompresses at build time.

# Layer 1: Base image
FROM docker.io/library/ubuntu:24.04

LABEL maintainer="Claude Code Recreation"
LABEL description="Recreated Claude Code web development environment"

# Redirect RUN output to avoid SIGPIPE under gVisor+buildah (requires --format=docker).
# On failure, copy last 200 lines to fd 3 (the original stderr, saved before redirect).
SHELL ["/bin/bash", "-c", "exec 3>&2; set -euo pipefail; trap 'rc=$?; tail -200 /tmp/build-step.log >&3; exit $rc' ERR; exec > /tmp/build-step.log 2>&1; eval \"$0\""]

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# =============================================================================
# APT SETUP (Layers 2-6)
# =============================================================================

# Layer 2: CA certificates for TLS-inspecting proxy
COPY rootfs/usr/local/share/ca-certificates/*.crt /usr/local/share/ca-certificates/

# Layer 3: APT source files (PHP PPA, deadsnakes PPA)
COPY rootfs/etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/

# Layer 4: APT preference files (version pins)
COPY rootfs/etc/apt/preferences.d/* /etc/apt/preferences.d/

# Layer 5: Configure APT snapshot repos + install ca-certificates
RUN rm -f /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list && \
    printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/20251201T000000Z\nSuites: noble noble-updates noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/snapshot.sources && \
    printf '%s\n' 'Package: *' 'Pin: origin snapshot.ubuntu.com' 'Pin-Priority: 1001' \
      > /etc/apt/preferences.d/snapshot-pin && \
    printf '%s\n' 'Package: libglib2.0-* gir1.2-glib-2.0 libgirepository-2.0-0' \
      'Pin: version 2.80.0-6ubuntu3.8' 'Pin-Priority: 1002' \
      > /etc/apt/preferences.d/glib-pin && \
    printf '%s\n' \
      'Package: libreoffice-* fonts-opensymbol libuno-* uno-libs-private ure python3-uno libreoffice-style-* libreoffice-uiconfig-*' \
      'Pin: version 4:24.2*' 'Pin-Priority: 1003' \
      > /etc/apt/preferences.d/libreoffice-pin && \
    printf '%s\n' 'Package: postgresql-* libpq5 libpq-dev' \
      'Pin: version 16.11*' 'Pin-Priority: 1003' \
      > /etc/apt/preferences.d/postgresql-pin && \
    printf 'Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble noble-updates noble-security\nComponents: main restricted universe multiverse\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/live-archive.sources && \
    printf '%s\n' 'Package: *' 'Pin: origin archive.ubuntu.com' 'Pin-Priority: 100' \
      > /etc/apt/preferences.d/live-archive-pin && \
    printf '%s\n' 'Package: *' 'Pin: origin ppa.launchpadcontent.net' 'Pin-Priority: 1002' \
      > /etc/apt/preferences.d/ppa-pin && \
    echo 'Acquire::https::Verify-Peer "false";' > /etc/apt/apt.conf.d/99no-verify && \
    printf 'Acquire::Retries "3";\nAcquire::https::Timeout "30";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80retries && \
    for attempt in 1 2 3 4 5; do \
      rm -rf /var/lib/apt/lists/* && \
      apt-get update 2>&1 | tee /tmp/apt-update.log && \
      if ! grep -qi 'Failed to fetch\|Could not\|Bad Gateway\|Internal Server Error' /tmp/apt-update.log; then break; fi; \
      echo "apt-get update had partial failures (attempt $attempt/5), retrying in 15s..."; \
      sleep 15; \
    done && \
    apt-get install --allow-downgrades -y gpgv libsystemd0 libudev1 && \
    apt-get install --allow-downgrades -y --no-install-recommends ca-certificates && \
    update-ca-certificates && \
    rm /etc/apt/apt.conf.d/99no-verify

ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Layer 6: Core packages (base system, build tools, clang, libs, systemd, podman, fonts, java, postgres, redis)
# NOTE: apt lists persist from Layer 5 - no apt-get update needed here
RUN apt-get install --allow-downgrades -y --no-install-recommends \
    software-properties-common gnupg2 curl \
    adduser age apt-transport-https base-files bash bc \
    bsdutils bzip2 ca-certificates coreutils dash diffutils dpkg file findutils \
    gzip hostname less locales login lsof m4 mawk mount nano netbase \
    netcat-openbsd passwd patch perl procps psmisc sed sensible-utils \
    sudo tar tmux tzdata unzip wget xz-utils zip util-linux \
    autoconf automake autotools-dev binutils bison build-essential \
    cmake cmake-data cpp cpp-13 dpkg-dev g++ g++-13 gcc gcc-13 \
    gdb libc6-dev libtool lto-disabled-list make ninja-build pkg-config \
    clang clang-18 clang-format clang-format-18 clang-tidy clang-tidy-18 \
    clang-tools-18 lld lld-18 lldb lldb-18 llvm llvm-18 llvm-18-runtime \
    libssl-dev libssl3t64 openssl \
    libsqlite3-dev libreadline-dev libncurses-dev \
    libbz2-dev libffi-dev zlib1g-dev \
    libyaml-dev libgdbm-dev libgdbm-compat-dev libdb-dev uuid-dev liblzma-dev \
    libpng-dev libpng16-16t64 libbrotli-dev \
    libblkid-dev libblkid1 libmount-dev libmount1 \
    libselinux1-dev libsepol-dev \
    libcairo2-dev libpixman-1-dev libxcb-render0-dev libxcb-shm0-dev \
    libxrender-dev libxext-dev libfontconfig-dev libfreetype-dev \
    libglib2.0-dev libglib2.0-dev-bin libglib2.0-0t64 libglib2.0-bin libglib2.0-data \
    x11proto-core-dev git \
    gnupg gnupg-utils gpg gpg-agent gpgconf gpgsm gpgv dirmngr keyboxd \
    jq ripgrep strace valgrind vim xvfb yq \
    systemd systemd-dev systemd-sysv libsystemd0 libsystemd-shared libudev1 libpam-systemd \
    podman buildah crun conmon catatonit fuse-overlayfs fuse3 \
    slirp4netns passt netavark aardvark-dns uidmap containernetworking-plugins \
    fonts-dejavu-core fonts-dejavu-mono fonts-freefont-ttf fonts-ipafont-gothic \
    fonts-liberation fonts-noto-color-emoji fonts-opensymbol fonts-tlwg-loma-otf \
    fonts-unifont fonts-wqy-zenhei xfonts-cyrillic xfonts-scalable \
    libreoffice-core libreoffice-common python3-uno \
    libjs-jquery libjs-sphinxdoc libjs-underscore nftables libpython3-dev python3-dev \
    openjdk-21-jdk openjdk-21-jre ca-certificates-java \
    postgresql-16 postgresql-common postgresql-client-16 redis-server redis-tools \
    libdrm2 libdrm-amdgpu1 libdrm-intel1 libdrm-common \
    libxml2 libxml2-utils libfdisk1 libsmartcols1 libuuid1 \
    libxslt1.1 libsodium23 libtasn1-6 \
    libavahi-client3 libavahi-common3 libavahi-common-data \
    libheif1 libheif-plugin-aomdec libheif-plugin-libde265 \
    libgirepository-2.0-0 mesa-libgallium libgbm1 libgl1-mesa-dri libglx-mesa0 \
    linux-libc-dev

# Layer 7: Python versions from deadsnakes
# NOTE: apt lists persist from Layer 5 (includes deadsnakes PPA indexes)
RUN apt-get install --allow-downgrades -y --no-install-recommends \
    python3.10 python3.10-dev python3.10-venv python3.10-distutils \
    python3.11 python3.11-dev python3.11-venv python3.11-distutils \
    python3.12=3.12.3-1ubuntu0.11 python3.12-dev=3.12.3-1ubuntu0.11 \
    python3.12-venv=3.12.3-1ubuntu0.11 python3.12-minimal=3.12.3-1ubuntu0.11 \
    libpython3.12-minimal=3.12.3-1ubuntu0.11 libpython3.12-stdlib=3.12.3-1ubuntu0.11 \
    libpython3.12-dev=3.12.3-1ubuntu0.11 libpython3.12t64=3.12.3-1ubuntu0.11 \
    python3.13 python3.13-dev python3.13-venv \
    python3-pip python3-setuptools python3-wheel \
    python3-apt=2.7.7ubuntu5.2 python-apt-common=2.7.7ubuntu5.2

# Layer 8: PHP 8.4
# NOTE: apt lists persist from Layer 5 (includes PHP PPA indexes)
RUN apt-get install --allow-downgrades -y --no-install-recommends \
    php-common php8.4-cli php8.4-common php8.4-curl php8.4-dev php8.4-gd \
    php8.4-intl php8.4-mbstring php8.4-mysql php8.4-opcache \
    php8.4-pgsql php8.4-readline php8.4-sqlite3 php8.4-xml php8.4-zip \
    php8.4-igbinary php8.4-redis \
    && rm -rf /var/lib/apt/lists/*

# Layer 8b: Docker CE (from download.docker.com, not snapshot repos)
COPY rootfs/etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.asc
COPY rootfs/etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.list
RUN apt-get update && \
    apt-get install --allow-downgrades -y --no-install-recommends \
    containerd.io=2.2.1-1~ubuntu.24.04~noble \
    docker-ce=5:29.2.1-1~ubuntu.24.04~noble \
    docker-ce-cli=5:29.2.1-1~ubuntu.24.04~noble \
    docker-buildx-plugin=0.31.1-1~ubuntu.24.04~noble \
    docker-compose-plugin=5.0.2-1~ubuntu.24.04~noble \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# RUNTIME INSTALLATIONS (Layers 9-19)
# =============================================================================

# Layer 9: Node.js 20, 21, 22 + NVM
RUN mkdir -p /opt && \
    curl -fsSL https://nodejs.org/dist/v20.20.0/node-v20.20.0-linux-x64.tar.xz | \
        tar -xJ -C /opt && mv /opt/node-v20.20.0-linux-x64 /opt/node20 && \
    curl -fsSL https://nodejs.org/dist/v21.7.3/node-v21.7.3-linux-x64.tar.xz | \
        tar -xJ -C /opt && mv /opt/node-v21.7.3-linux-x64 /opt/node21 && \
    curl -fsSL https://nodejs.org/dist/v22.22.0/node-v22.22.0-linux-x64.tar.xz | \
        tar -xJ -C /opt && mv /opt/node-v22.22.0-linux-x64 /opt/node22 && \
    mkdir -p /opt/nvm && \
    curl -fsSL https://github.com/nvm-sh/nvm/archive/refs/tags/v0.40.1.tar.gz | \
        tar -xz --strip-components=1 -C /opt/nvm

ENV PATH="/opt/node22/bin:$PATH"

# Layer 10: npm global packages
RUN npm install -g --ignore-scripts chromedriver@145.0.1 playwright@1.56.1 && \
    npm install -g \
    @anthropic-ai/claude-code@2.1.38 eslint@10.0.0 http-server@14.1.1 \
    nodemon@3.1.11 pnpm@10.29.2 prettier@3.8.1 serve@14.2.5 \
    ts-node@10.9.2 typescript@5.9.3 yarn@1.22.22

# Layer 11: rbenv + Ruby 3.1.6, 3.2.6, 3.3.6 + symlinks
RUN mkdir -p /opt/rbenv /opt/rbenv/plugins/ruby-build && \
    curl -fsSL https://github.com/rbenv/rbenv/archive/refs/heads/master.tar.gz | \
        tar -xz --strip-components=1 -C /opt/rbenv && \
    curl -fsSL https://github.com/rbenv/ruby-build/archive/refs/heads/master.tar.gz | \
        tar -xz --strip-components=1 -C /opt/rbenv/plugins/ruby-build && \
    for ver in 3.1.6 3.2.6 3.3.6; do \
        mkdir -p /opt/rbenv/versions/$ver && \
        curl -fsSL "https://github.com/ruby/ruby-builder/releases/download/toolcache/ruby-${ver}-ubuntu-24.04.tar.gz" | \
        tar -xz --strip-components=1 -C /opt/rbenv/versions/$ver; \
    done && \
    ln -s /opt/rbenv/versions/3.1.6 /opt/ruby-3.1.6 && \
    ln -s /opt/rbenv/versions/3.2.6 /opt/ruby-3.2.6 && \
    ln -s /opt/rbenv/versions/3.3.6 /opt/ruby-3.3.6

ENV RBENV_ROOT=/opt/rbenv
ENV PATH="$RBENV_ROOT/bin:$RBENV_ROOT/shims:$PATH"

# Layer 12: Initialize rbenv
RUN eval "$(rbenv init -)" && rbenv global 3.3.6 && rbenv rehash

# Layer 13: Go 1.24.7, 1.25.1 + golangci-lint + version scripts
RUN curl -fsSL https://go.dev/dl/go1.24.7.linux-amd64.tar.gz | tar -xz -C /usr/local && \
    mv /usr/local/go /usr/local/go1.24.7 && \
    curl -fsSL https://go.dev/dl/go1.25.1.linux-amd64.tar.gz | tar -xz -C /usr/local && \
    mv /usr/local/go /usr/local/go1.25.1 && \
    printf '#!/bin/bash\nexport PATH="/usr/local/go1.24.7/bin:${PATH//\/usr\/local\/go[0-9]*/}"\necho "Switched to Go $(go version)"\n' > /usr/local/use-go-1.24.7.sh && \
    printf '#!/bin/bash\nexport PATH="/usr/local/go1.25.1/bin:${PATH//\/usr\/local\/go[0-9]*/}"\necho "Switched to Go $(go version)"\n' > /usr/local/use-go-1.25.1.sh && \
    chmod +x /usr/local/use-go-1.24.7.sh /usr/local/use-go-1.25.1.sh && \
    curl -fsSL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh | sh -s -- -b /usr/local/bin v2.5.0

ENV PATH="$PATH:/usr/local/go1.25.1/bin"
ENV GOPATH="/root/go"

# Layer 14: Rust
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:$PATH"

# Layer 15: Bun
RUN curl -fsSL https://bun.sh/install | bash -s "bun-v1.3.9"
ENV BUN_INSTALL="/root/.bun"
ENV PATH="$BUN_INSTALL/bin:$PATH"

# Layer 16: UV + Python tools
RUN curl -fsSL https://astral.sh/uv/0.8.17/install.sh | sh && \
    /root/.local/bin/uv tool install black && \
    /root/.local/bin/uv tool install flake8 && \
    /root/.local/bin/uv tool install mypy && \
    /root/.local/bin/uv tool install poetry && \
    /root/.local/bin/uv tool install pyright && \
    /root/.local/bin/uv tool install pytest && \
    /root/.local/bin/uv tool install ruff
ENV PATH="/root/.local/bin:$PATH"
ENV UV_PYTHON_PREFERENCE=only-system

# Layer 17: Maven + Gradle + Composer
RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.tar.gz | \
        tar -xz -C /opt && ln -s /opt/apache-maven-3.9.11 /opt/maven && \
    curl -fsSL "https://services.gradle.org/distributions/gradle-8.14.3-bin.zip" -o /tmp/gradle.zip && \
        unzip -q /tmp/gradle.zip -d /opt && ln -s /opt/gradle-8.14.3 /opt/gradle && rm /tmp/gradle.zip && \
    curl -fsSL https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer

ENV MAVEN_HOME=/opt/maven
ENV GRADLE_HOME=/opt/gradle
ENV PATH="$MAVEN_HOME/bin:$GRADLE_HOME/bin:$PATH"

# Layer 18: pip packages
RUN pip3 install --break-system-packages \
    anyio==4.12.1 argcomplete==3.1.4 certifi==2025.11.12 charset-normalizer==3.4.4 \
    conan==2.23.0 colorama==0.4.6 h11==0.16.0 h2==4.3.0 httpcore==1.0.9 httpx==0.28.1 \
    idna==3.11 Jinja2==3.1.6 MarkupSafe==3.0.3 packaging==24.0 patch-ng==1.18.1 \
    pre-commit PyYAML==6.0.1 requests==2.32.5 toml==0.10.2 typing_extensions==4.15.0 \
    urllib3==2.6.1 virtualenv websockets==16.0 xmltodict==0.13.0 yq==3.1.0

# Layer 19: Symlinks in /usr/local/bin
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

# =============================================================================
# SCRIPTS AND CONFIGURATION (Layers 20-28)
# =============================================================================

# Layer 20: Profile scripts (all in one directory copy)
COPY rootfs/etc/profile.d/ /etc/profile.d/

# Layer 21: Helper scripts
COPY rootfs/usr/local/bin/use-python rootfs/usr/local/bin/use-node-20 rootfs/usr/local/bin/use-node-21 \
     rootfs/usr/local/bin/use-node-22 rootfs/usr/local/bin/use-ruby-3.1.6 rootfs/usr/local/bin/use-ruby-3.2.6 \
     rootfs/usr/local/bin/use-ruby-3.3.6 rootfs/usr/local/bin/create-venv-py3.10 rootfs/usr/local/bin/create-venv-py3.11 \
     rootfs/usr/local/bin/create-venv-py3.12 rootfs/usr/local/bin/create-venv-py3.13 \
     rootfs/usr/local/bin/check-tools rootfs/usr/local/bin/use-php-8.4 /usr/local/bin/

# Layer 22: Compressed binaries
COPY reference/environment-manager.gz reference/process_api.gz /tmp/

# Layer 23: Extract binaries + chmod all helper scripts
RUN chmod +x /usr/local/bin/use-python /usr/local/bin/use-node-* /usr/local/bin/use-ruby-* \
        /usr/local/bin/create-venv-* /usr/local/bin/check-tools /usr/local/bin/use-php-8.4 && \
    gunzip -c /tmp/environment-manager.gz > /usr/local/bin/environment-manager && \
    chmod +x /usr/local/bin/environment-manager && \
    gunzip -c /tmp/process_api.gz > /process_api && \
    chmod +x /process_api && \
    rm /tmp/*.gz

# Layer 24: Root dotfiles and env script
COPY rootfs/root/.bashrc rootfs/root/.profile rootfs/root/.wget-hsts rootfs/root/.gitconfig /root/
COPY rootfs/root/.local/bin/env /root/.local/bin/env

# Layer 25: Root Claude config
COPY rootfs/root/.claude/ /root/.claude/

# Layer 26: User setup + directories + copy gitconfig
RUN useradd -m -s /bin/bash -u 999 claude && \
    mkdir -p /home/user /home/claude/.claude /home/claude/.ssh /home/claude/scripts && \
    chmod +x /root/.local/bin/env && \
    cp /root/.gitconfig /home/claude/.gitconfig && \
    chown claude:claude /home/claude/.gitconfig

# Layer 27: Claude user home files
COPY rootfs/home/claude/README.md /home/claude/README.md
COPY rootfs/home/claude/scripts/README.md /home/claude/scripts/README.md
COPY rootfs/home/claude/.claude/ /home/claude/.claude/

# Layer 28: Claude user permissions
RUN chmod +x /home/claude/.claude/stop-hook-git-check.sh && \
    chown -R claude:claude /home/claude/.claude

# =============================================================================
# FINAL SETUP (Layers 29-31)
# =============================================================================

# Layer 29: SUID/SGID bits + directory permissions
RUN chmod 4755 /usr/bin/chfn /usr/bin/chsh /usr/bin/gpasswd /usr/bin/newgrp \
               /usr/bin/passwd /usr/bin/sudo \
               /usr/lib/dbus-1.0/dbus-daemon-launch-helper \
               /usr/lib/polkit-1/polkit-agent-helper-1 && \
    chmod 2755 /usr/bin/chage /usr/bin/expiry \
               /usr/sbin/pam_extrausers_chkpwd /usr/sbin/unix_chkpwd && \
    (chmod 4755 /usr/lib/x86_64-linux-gnu/gstreamer1.0/gstreamer-1.0/gst-ptp-helper 2>/dev/null || true) && \
    (chmod 2755 /usr/lib/x86_64-linux-gnu/utempter/utempter 2>/dev/null || true) && \
    chmod 2770 /etc/redis && \
    chmod 1733 /var/lib/php/sessions && \
    chmod 2775 /var/local /var/mail /usr/local/share/fonts && \
    chmod 555 /

# Layer 30: Symlinks + alternatives + runtime directories
RUN ln -sf /proc/mounts /etc/mtab && \
    update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 130 && \
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-13 130 && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    mkdir -p /root/.local/share/gem /root/.local/lib/python3.11

# Layer 31: Remove build-time APT sources (not present in live container)
# NOTE: Do NOT strip /usr/share/doc, /usr/share/man, /usr/include, etc.
# These exist in the live container and should remain for reproducibility.
RUN rm -f \
    /etc/apt/sources.list.d/snapshot.sources \
    /etc/apt/sources.list.d/live-archive.sources \
    /etc/apt/preferences.d/snapshot-pin \
    /etc/apt/preferences.d/live-archive-pin \
    /etc/apt/preferences.d/glib-pin \
    /etc/apt/preferences.d/libreoffice-pin \
    /etc/apt/preferences.d/postgresql-pin \
    /etc/apt/preferences.d/ppa-pin \
    /etc/apt/preferences.d/php84-pin \
    /etc/apt/preferences.d/version-drift-pin

# =============================================================================
# ENVIRONMENT
# =============================================================================

ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/user

CMD ["/bin/bash"]
