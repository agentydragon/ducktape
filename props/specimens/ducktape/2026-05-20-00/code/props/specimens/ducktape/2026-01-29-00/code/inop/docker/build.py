#!/usr/bin/env python3
import subprocess

# Build claude-base
subprocess.run(["docker", "build", "-t", "claude-base:latest", "-f", "docker/claude-base/Dockerfile", "."], check=True)

# Build all dev environments
for env in ["python", "python-data", "rust", "node", "go", "ruby", "system"]:
    subprocess.run(["docker", "build", "-t", f"claude-dev:{env}", "-f", f"docker/{env}/Dockerfile", "."], check=True)
