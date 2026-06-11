FROM claude-base:latest

# Switch to root for installations
USER root

# Install Ruby and development tools
RUN apt-get update && apt-get install -y \
    ruby-full \
    ruby-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install common Ruby gems system-wide (one at a time for better error visibility)
RUN gem install bundler
RUN gem install json
RUN gem install httparty
RUN gem install colorize
# Skip sqlite3 as it often has native extension issues

# Switch back to node user
USER node

LABEL dev.environment="ruby"
LABEL dev.description="Ruby development environment with common gems"