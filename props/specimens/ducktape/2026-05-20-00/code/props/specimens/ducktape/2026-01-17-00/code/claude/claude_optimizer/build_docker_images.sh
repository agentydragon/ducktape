#!/bin/bash
# Build all Docker images for Claude Optimizer

set -e # Exit on error

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Building Claude Optimizer Docker images...${NC}"

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Build claude-base first (other images depend on it)
echo -e "\n${GREEN}Building claude-base...${NC}"
docker build -t claude-base:latest docker/claude-base/ || {
  echo -e "${RED}Failed to build claude-base${NC}"
  exit 1
}

# Find all other Dockerfiles and build them
echo -e "\n${YELLOW}Finding and building remaining Docker images...${NC}"
for dockerfile in docker/*/Dockerfile; do
  # Skip claude-base since we already built it
  if [[ "$dockerfile" == "docker/claude-base/Dockerfile" ]]; then
    continue
  fi

  # Extract directory and image name
  dir=$(dirname "$dockerfile")
  img_name=$(basename "$dir")
  tag="claude-dev:${img_name}"

  echo -e "\n${GREEN}Building $tag from $dockerfile...${NC}"
  docker build -t "$tag" "$dir/" || {
    echo -e "${RED}Failed to build $tag${NC}"
    exit 1
  }
done

echo -e "\n${GREEN}All images built successfully!${NC}"
echo -e "\n${YELLOW}Available images:${NC}"
docker images | grep -E "^(claude-base|claude-dev)" | awk '{printf "  %-30s %s\n", $1":"$2, $7" "$8" "$9}'
