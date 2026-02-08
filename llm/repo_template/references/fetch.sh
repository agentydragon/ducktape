#!/bin/bash
# This script fetches reference documentation and code for this project
# Run this to update all reference materials

set -euo pipefail

cd "$(dirname "$0")"

echo "Fetching reference materials..."

# Example: Fetch Python documentation
# echo "Fetching Python typing documentation..."
# curl -L https://docs.python.org/3/library/typing.html -o python-typing.html 2>/dev/null || echo "Failed to fetch Python typing docs"

# Example: Clone specific reference implementations
# echo "Cloning reference implementations..."
# if [ ! -d "fastapi-example" ]; then
#     git clone --depth 1 https://github.com/tiangolo/fastapi.git fastapi-example
# fi

# Example: Download specific files
# echo "Downloading specific reference files..."
# curl -O https://raw.githubusercontent.com/psf/black/main/pyproject.toml

# Common Python reference materials
echo "Fetching Python best practices..."
curl -L https://docs.python-guide.org/writing/structure/ -o python-project-structure.html 2>/dev/null || echo "Failed to fetch Python structure guide"

# Fetch common Python tooling configs
echo "Fetching example Python configurations..."
curl -L https://raw.githubusercontent.com/psf/black/main/pyproject.toml -o black-pyproject-example.toml 2>/dev/null || echo "Failed to fetch Black config"
curl -L https://raw.githubusercontent.com/charliermarsh/ruff/main/pyproject.toml -o ruff-pyproject-example.toml 2>/dev/null || echo "Failed to fetch Ruff config"

# FastAPI reference (common web framework)
echo "Cloning FastAPI examples..."
if [ ! -d "fastapi-examples" ]; then
  git clone --depth 1 --filter=blob:none --sparse https://github.com/tiangolo/fastapi.git fastapi-examples 2>/dev/null || echo "Failed to clone FastAPI"
  cd fastapi-examples && git sparse-checkout set docs_src/first_steps docs_src/tutorial 2>/dev/null || true
  cd ..
fi

# Pytest examples
echo "Fetching pytest documentation..."
curl -L https://docs.pytest.org/en/stable/getting-started.html -o pytest-getting-started.html 2>/dev/null || echo "Failed to fetch pytest docs"

echo "Reference fetch complete!"
