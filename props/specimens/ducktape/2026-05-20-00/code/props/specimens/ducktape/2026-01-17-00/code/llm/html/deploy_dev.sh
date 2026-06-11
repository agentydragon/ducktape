#!/bin/bash
# Development deployment script for llm_html service
# Deploys the current working tree (including uncommitted changes) to production

set -euo pipefail

# Configuration
VPS_HOST="root@agentydragon.com" # Assumes SSH config has this host configured
REMOTE_BUILD_DIR="/tmp/llm-html-build"
CONTAINER_NAME="llm_html" # Same as production container
IMAGE_NAME="llm-html:dev"
HOST_PORT="9000" # Same port as production

echo "🚀 Starting deployment of current working tree to production..."

# Run unit tests first
echo "🧪 Running unit tests..."
if ! python -m pytest test_*.py -v; then
  echo "❌ Unit tests failed! Aborting deployment."
  echo "Fix the failing tests before deploying."
  exit 1
fi
echo "✅ All tests passed!"

echo "⚠️  WARNING: This will replace the production container at llm.agentydragon.com"
read -p "Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 1
fi

# Copy coding instructions to current directory for Docker build
echo "📋 Copying coding instructions..."
cp ../../dotfiles/codex/instructions.md coding.md

# Create a tarball of the current working directory
echo "📦 Creating archive of current working tree..."
# Include all necessary files (tracked, staged, and untracked)
tar -czf /tmp/llm-html-dev.tar.gz \
  --exclude=__pycache__ \
  --exclude=.pytest_cache \
  --exclude=*.pyc \
  --exclude=.git \
  *.py *.md *.html *.css *.txt *.sh Dockerfile requirements.txt

# Clean up temporary file
rm -f coding.md

# Copy the tarball to VPS
echo "📤 Copying files to VPS..."
scp /tmp/llm-html-dev.tar.gz $VPS_HOST:$REMOTE_BUILD_DIR.tar.gz

# Clean up local tarball
rm -f /tmp/llm-html-dev.tar.gz

# Build and deploy on VPS
echo "🔨 Building and deploying on VPS..."
ssh $VPS_HOST <<'EOF'
set -euo pipefail

# Try to get existing TOKEN_SECRET from running container
SECRET=$(docker inspect llm_html 2>/dev/null | jq -r '.[0].Config.Env[] | select(startswith("TOKEN_SECRET=")) | split("=")[1]' || echo "")
if [ -z "$SECRET" ]; then
    echo "⚠️  No existing TOKEN_SECRET found, generating new one..."
    SECRET=$(openssl rand -hex 32)
fi

# Extract files
echo "📂 Extracting files..."
rm -rf /tmp/llm-html-build
mkdir -p /tmp/llm-html-build
cd /tmp/llm-html-build
tar -xzf ../llm-html-build.tar.gz

# Create a temporary Dockerfile for dev deployment
cat > Dockerfile.dev << 'DOCKERFILE_EOF'
FROM python:3.11-slim AS runtime

# Avoid buffering Python stdout/stderr
ENV PYTHONUNBUFFERED=1

# Install curl for health-check ping
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Only copy dependency manifests first to leverage Docker layer cache
COPY pyproject.toml requirements.txt ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the application code from the build context
COPY llm_html/ ./llm_html/
COPY index.md style.css base.html verify.html stats.html tana.md coding.md ./

EXPOSE 9000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl --fail http://127.0.0.1:9000/ || exit 1

CMD ["python", "-m", "llm_html.server"]
DOCKERFILE_EOF

# Build Docker image
echo "🐳 Building Docker image..."
docker build -t llm-html:dev -f Dockerfile.dev .

# Clean up temporary Dockerfile
rm -f Dockerfile.dev

# Stop and remove existing container
echo "🛑 Stopping existing production container..."
docker stop llm_html 2>/dev/null || true
docker rm llm_html 2>/dev/null || true

# Run new container with production settings
echo "🚀 Starting new container..."
docker run -d \
  --name llm_html \
  --restart unless-stopped \
  -p 9000:9000 \
  -e TOKEN_SECRET="$SECRET" \
  -e SITE_URL="http://llm.agentydragon.com" \
  llm-html:dev

# Clean up
echo "🧹 Cleaning up..."
rm -rf /tmp/llm-html-build /tmp/llm-html-build.tar.gz

echo "✅ Deployment complete!"
EOF

echo "
✨ Deployment finished!

Your current working tree is now live at: http://llm.agentydragon.com

To check logs: ssh vps 'docker logs -f ${CONTAINER_NAME}'
"
