FROM claude-base:latest

USER root

# Install Python development environment
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

USER node

# Install common Python packages in user space
RUN pip3 install --user --no-cache-dir \
    requests \
    flask \
    fastapi \
    uvicorn \
    pytest \
    black \
    ruff \
    mypy

# Add user pip bin to PATH
ENV PATH="/home/node/.local/bin:${PATH}"


LABEL dev.environment="python"
LABEL dev.description="Python development environment with common packages"
