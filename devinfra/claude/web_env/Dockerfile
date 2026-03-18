# Claude Code Web Container Recreation
#
# Build (Docker, recommended):
#   docker build --network=host \
#     --build-arg http_proxy="$https_proxy" \
#     --build-arg https_proxy="$https_proxy" \
#     -t claude-code-web-recreated .
#
# NOTE: Docker's overlay snapshotter in gVisor is limited to ~35 lowerdir
# entries. Ubuntu 24.04 base contributes 4 base layers, leaving ~31 slots.
# If layers are ever added and the build fails with "mount source: overlay...
# invalid argument", consolidate COPY/RUN instructions.
#
# Prerequisites:
#   1. Proprietary binaries must be captured first (run from within live container):
#        bazel run //claude_web_env/tools:build_and_diff -- --capture-binaries
#      This writes reference/environment-manager.gz and reference/process_api.gz.
#   2. local_debs/ must be populated by running fetch_debs.py (downloads from
#      Ubuntu snapshot archives + PPAs). build_and_diff.py calls this automatically.

FROM docker.io/library/ubuntu:24.04

LABEL maintainer="Claude Code Recreation"
LABEL description="Recreated Claude Code web development environment"

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# All ENV in one instruction to minimise overlay layer count (gVisor limit ~35).
# PATH is fully hardcoded (not referencing $PATH) to avoid expansion ambiguity.
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    RBENV_ROOT=/opt/rbenv \
    BUN_INSTALL=/root/.bun \
    UV_PYTHON_PREFERENCE=only-system \
    MAVEN_HOME=/opt/maven \
    GRADLE_HOME=/opt/gradle \
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    GOPATH=/root/go \
    PATH="/root/.local/bin:/root/.cargo/bin:/root/.bun/bin:/opt/rbenv/bin:/opt/rbenv/shims:/opt/node22/bin:/opt/maven/bin:/opt/gradle/bin:/usr/local/go1.25.1/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# =============================================================================
# STATIC FILES (Layers 3-4)
# =============================================================================

# Layer 3: All /etc/ overlay files (apt repos, preferences, keyrings, profile.d)
# Includes: CA certs for TLS proxy, PPA sources, preferences, docker.asc/list,
# profile.d scripts. All placed before any RUN step so APT can use them.
COPY rootfs/etc/ /etc/

# Layer 4: All /usr/ overlay files (CA certs for TLS proxy, helper scripts)
# Includes: /usr/local/share/ca-certificates/*.crt and /usr/local/bin/ scripts.
# CA certs must be here before the first RUN that runs update-ca-certificates.
COPY rootfs/usr/ /usr/

# =============================================================================
# PACKAGE INSTALLATION
# =============================================================================

# All dpkg packages — fetched from Ubuntu snapshot archives and PPAs by
# fetch_debs.py. This guarantees exact version match with the live environment
# and works from any machine (no dpkg-repack or live container needed).
# The build script (build_and_diff.py) calls fetch_debs automatically.
# local_debs/ is .gitignored — it's a build artifact.
COPY local_debs/ /tmp/local_debs/
# Force-install all debs (ignoring postinst failures on first pass), then
# repeatedly configure to let post-install scripts succeed as deps become available.
RUN dpkg --force-all -i /tmp/local_debs/*.deb || true && \
    rm -rf /tmp/local_debs && \
    for i in 1 2 3 4 5; do \
      if dpkg --configure -a 2>&1; then break; fi; \
      echo "=== configure pass $i had failures, retrying... ==="; \
    done && \
    update-ca-certificates
# =============================================================================
# RUNTIME INSTALLATIONS
# =============================================================================

# Node.js 20, 21, 22 + NVM
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

# npm global packages
RUN npm install -g --ignore-scripts chromedriver@145.0.1 playwright@1.56.1 && \
    npm install -g \
    @anthropic-ai/claude-code@2.1.38 eslint@10.0.0 http-server@14.1.1 \
    nodemon@3.1.11 pnpm@10.29.2 prettier@3.8.1 serve@14.2.5 \
    ts-node@10.9.2 typescript@5.9.3 yarn@1.22.22

# rbenv + Ruby 3.1.6, 3.2.6, 3.3.6 + symlinks
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

# Initialize rbenv
RUN eval "$(rbenv init -)" && rbenv global 3.3.6 && rbenv rehash

# Go 1.24.7, 1.25.1 + golangci-lint + version scripts
RUN curl -fsSL https://go.dev/dl/go1.24.7.linux-amd64.tar.gz | tar -xz -C /usr/local && \
    mv /usr/local/go /usr/local/go1.24.7 && \
    curl -fsSL https://go.dev/dl/go1.25.1.linux-amd64.tar.gz | tar -xz -C /usr/local && \
    mv /usr/local/go /usr/local/go1.25.1 && \
    printf '#!/bin/bash\nexport PATH="/usr/local/go1.24.7/bin:${PATH//\/usr\/local\/go[0-9]*/}"\necho "Switched to Go $(go version)"\n' > /usr/local/use-go-1.24.7.sh && \
    printf '#!/bin/bash\nexport PATH="/usr/local/go1.25.1/bin:${PATH//\/usr\/local\/go[0-9]*/}"\necho "Switched to Go $(go version)"\n' > /usr/local/use-go-1.25.1.sh && \
    chmod +x /usr/local/use-go-1.24.7.sh /usr/local/use-go-1.25.1.sh && \
    curl -fsSL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh | sh -s -- -b /usr/local/bin v2.5.0

# Rust
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal

# Bun
RUN curl -fsSL https://bun.sh/install | bash -s "bun-v1.3.9"

# UV + Python tools
RUN curl -fsSL https://astral.sh/uv/0.8.17/install.sh | sh && \
    /root/.local/bin/uv tool install black && \
    /root/.local/bin/uv tool install flake8 && \
    /root/.local/bin/uv tool install mypy && \
    /root/.local/bin/uv tool install poetry && \
    /root/.local/bin/uv tool install pyright && \
    /root/.local/bin/uv tool install pytest && \
    /root/.local/bin/uv tool install ruff

# Maven + Gradle + Composer
RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.tar.gz | \
        tar -xz -C /opt && ln -s /opt/apache-maven-3.9.11 /opt/maven && \
    curl -fsSL "https://services.gradle.org/distributions/gradle-8.14.3-bin.zip" -o /tmp/gradle.zip && \
        unzip -q /tmp/gradle.zip -d /opt && ln -s /opt/gradle-8.14.3 /opt/gradle && rm /tmp/gradle.zip && \
    curl -fsSL https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer

# pip packages
RUN pip3 install --break-system-packages \
    anyio==4.12.1 argcomplete==3.1.4 certifi==2025.11.12 charset-normalizer==3.4.4 \
    conan==2.23.0 colorama==0.4.6 h11==0.16.0 h2==4.3.0 httpcore==1.0.9 httpx==0.28.1 \
    idna==3.11 Jinja2==3.1.6 MarkupSafe==3.0.3 packaging==24.0 patch-ng==1.18.1 \
    pre-commit PyYAML==6.0.1 requests==2.32.5 toml==0.10.2 typing_extensions==4.15.0 \
    urllib3==2.6.1 virtualenv websockets==16.0 xmltodict==0.13.0 yq==3.1.0

# Symlinks in /usr/local/bin
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
# SCRIPTS AND CONFIGURATION
# =============================================================================

# Compressed proprietary binaries
COPY reference/environment-manager.gz reference/process_api.gz /tmp/

# Extract binaries + chmod all helper scripts
# Helper scripts are pre-placed in /usr/local/bin/ via Layer 4 (COPY rootfs/usr/)
RUN chmod +x /usr/local/bin/use-python /usr/local/bin/use-node-* /usr/local/bin/use-ruby-* \
        /usr/local/bin/create-venv-* /usr/local/bin/check-tools /usr/local/bin/use-php-8.4 && \
    gunzip -c /tmp/environment-manager.gz > /usr/local/bin/environment-manager && \
    chmod +x /usr/local/bin/environment-manager && \
    gunzip -c /tmp/process_api.gz > /process_api && \
    chmod +x /process_api && \
    rm /tmp/*.gz

# Root dotfiles, env script, and Claude config
COPY rootfs/root/ /root/

# User setup + directories + copy gitconfig
RUN useradd -m -s /bin/bash -u 999 claude && \
    mkdir -p /home/user /home/claude/.claude /home/claude/.ssh /home/claude/scripts && \
    chmod +x /root/.local/bin/env && \
    cp /root/.gitconfig /home/claude/.gitconfig && \
    chown claude:claude /home/claude/.gitconfig

# Claude user home files + permissions
COPY rootfs/home/claude/ /home/claude/

# =============================================================================
# FINAL SETUP
# =============================================================================

# Permission fixes (claude .claude + SUID/SGID + directory perms)
RUN chmod +x /home/claude/.claude/stop-hook-git-check.sh && \
    chown -R claude:claude /home/claude/.claude && \
    chmod 4755 /usr/bin/chfn /usr/bin/chsh /usr/bin/gpasswd /usr/bin/newgrp \
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

# Symlinks + alternatives + runtime dirs
# NOTE: Do NOT strip /usr/share/doc, /usr/share/man, /usr/include, etc.
# These exist in the live container and should remain for reproducibility.
RUN ln -sf /proc/mounts /etc/mtab && \
    update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 130 && \
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-13 130 && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    mkdir -p /root/.local/share/gem /root/.local/lib/python3.11

WORKDIR /home/user

CMD ["/bin/bash"]
