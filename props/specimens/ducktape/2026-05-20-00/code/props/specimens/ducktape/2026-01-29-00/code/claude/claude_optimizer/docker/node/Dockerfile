FROM claude-base:latest

# Switch to root for global npm installs
USER root

# Install global npm packages
RUN npm install -g \
    typescript \
    ts-node \
    @types/node \
    jest \
    eslint \
    prettier \
    nodemon

# Switch back to node user
USER node

LABEL dev.environment="node"
LABEL dev.description="Node.js development environment with TypeScript and common tools"