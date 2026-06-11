FROM claude-base:latest

# Switch to root for installations
USER root

# Install Python with data science packages
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set Python as default
RUN ln -sf /usr/bin/python3 /usr/bin/python

# Switch back to node user
USER node

# Install data science packages in user space
RUN pip3 install --user --no-cache-dir \
    pandas \
    numpy \
    scikit-learn \
    matplotlib \
    seaborn \
    jupyter \
    sqlalchemy \
    psycopg2-binary \
    requests \
    pytest \
    black \
    ruff

# Add user pip bin to PATH
ENV PATH="/home/node/.local/bin:${PATH}"

LABEL dev.environment="python-data"
LABEL dev.description="Python data science environment with pandas, numpy, sklearn"
