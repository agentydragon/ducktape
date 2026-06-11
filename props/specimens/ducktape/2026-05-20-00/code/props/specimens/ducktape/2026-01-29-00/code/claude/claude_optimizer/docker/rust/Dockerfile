FROM claude-base:latest

# Switch to root for installations
USER root

# Install system dependencies for common Rust crates
RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Switch back to node user
USER node

# Install Rust for the node user
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/home/node/.cargo/bin:${PATH}"

# Install common Rust tools
RUN /home/node/.cargo/bin/cargo install cargo-watch cargo-edit cargo-audit

LABEL dev.environment="rust"
LABEL dev.description="Rust development environment with cargo tools"