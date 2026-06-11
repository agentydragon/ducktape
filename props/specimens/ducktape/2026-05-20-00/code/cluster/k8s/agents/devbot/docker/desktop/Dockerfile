FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install desktop environment and dependencies
RUN apt-get update && apt-get install -y \
    xfce4 \
    xfce4-terminal \
    tigervnc-standalone-server \
    tigervnc-common \
    dbus-x11 \
    xfonts-base \
    xfonts-100dpi \
    xfonts-75dpi \
    xfonts-cyrillic \
    firefox \
    git \
    curl \
    wget \
    vim \
    nano \
    htop \
    python3 \
    python3-pip \
    # X11 libraries needed by MCP server
    libx11-6 \
    libxext6 \
    libxrandr2 \
    libxfixes3 \
    libxinerama1 \
    libxcursor1 \
    libxi6 \
    libxtst6 \
    # Additional development tools
    build-essential \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Create devbot user
RUN useradd -m -s /bin/bash devbot && \
    echo "devbot:devbot" | chpasswd && \
    mkdir -p /home/devbot/.vnc

# Setup VNC
COPY xstartup /home/devbot/.vnc/xstartup
RUN chmod +x /home/devbot/.vnc/xstartup && \
    chown -R devbot:devbot /home/devbot

# Create workspace directory
RUN mkdir -p /home/devbot/workspace && \
    chown -R devbot:devbot /home/devbot/workspace

USER devbot
WORKDIR /home/devbot

# VNC startup script
COPY --chown=devbot:devbot entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5900

ENTRYPOINT ["/entrypoint.sh"]
