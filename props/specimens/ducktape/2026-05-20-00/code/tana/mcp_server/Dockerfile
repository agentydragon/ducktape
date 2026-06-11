FROM ubuntu:24.04
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV BROWSER=/usr/bin/epiphany-browser
ENV WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
ENV GTK_A11Y=none

# Electron / Chromium runtime deps, Xvfb, x11vnc, noVNC, a lightweight window
# manager, and a GUI browser for Tana's "continue in browser" login handoff.
# Ubuntu 24.04 ships chromium as a snap transition package, so use
# epiphany-browser for an apt-installable GUI browser inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify xdg-utils epiphany-browser openbox xterm \
    # Electron / Chromium shared libraries
    libgtk-3-0 libnotify4 libnss3 libxss1 libasound2t64 \
    libgbm1 libsecret-1-0 libatk-bridge2.0-0 libatk1.0-0 \
    libdrm2 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 \
    libpango-1.0-0 libcairo2 libcups2 libdbus-1-3 libexpat1 \
    libxkbcommon0 \
    # Misc
    dbus-x11 fonts-noto-core ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash tana \
 && mkdir -p /home/tana/.config/Tana \
 && chown -R tana:tana /home/tana

# Install Tana Desktop from the official Debian package so desktop integration
# files (.desktop launcher, MIME/protocol handlers) are present in the image.
ARG TANA_VERSION=1.515.0
RUN curl -fsSL \
      "https://github.com/tanainc/tana-desktop-releases/releases/download/v${TANA_VERSION}/tana_${TANA_VERSION}_amd64.deb" \
      -o /tmp/tana.deb \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/tana.deb \
 && test -x /usr/bin/tana \
 && mkdir -p /opt/tana \
 && ln -sf /usr/bin/tana /opt/tana/Tana \
 && rm /tmp/tana.deb \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER tana
WORKDIR /home/tana

# 8262 = Tana MCP server, 6080 = noVNC web UI
EXPOSE 8262 6080

ENTRYPOINT ["/entrypoint.sh"]
