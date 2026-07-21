#!/bin/bash
set -e

# Default to local builds, optionally push to registry
REGISTRY="${REGISTRY:-local}"
PUSH="${PUSH:-false}"

if [ "$REGISTRY" = "local" ]; then
  DESKTOP_TAG="devbot-desktop:latest"
  MCP_TAG="computer-control-mcp:latest"
else
  DESKTOP_TAG="${REGISTRY}/devbot-desktop:latest"
  MCP_TAG="${REGISTRY}/computer-control-mcp:latest"
fi

# Build desktop image
echo "Building desktop image..."
docker build -t "${DESKTOP_TAG}" ./desktop/

# Build MCP server image
echo "Building MCP server image..."
docker build -t "${MCP_TAG}" ./mcp-server/

if [ "$PUSH" = "true" ]; then
  echo "Pushing images to ${REGISTRY}..."
  docker push "${DESKTOP_TAG}"
  docker push "${MCP_TAG}"
  echo "Done! Images pushed to ${REGISTRY}."
else
  echo "Done! Images built locally."
  echo ""
  echo "To push to Harbor, run:"
  echo "  REGISTRY=registry.allegedly.works/agents PUSH=true ./build.sh"
fi
