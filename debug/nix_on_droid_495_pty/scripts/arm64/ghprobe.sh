#!/bin/bash
# Which GitHub endpoints does the egress proxy serve for an UNATTACHED public
# repo? Nix's `github:` flake fetcher uses the tarball endpoints, so this
# decides whether `--flake github:...` can resolve its inputs at all here.
set -u
REV=df611d5358360092b1d2e7e756f94b23843bd9d6
probe() { printf '%-72s %s\n' "$1" "$(curl -s -o /dev/null -w '%{http_code}' -m 60 -L "$1")"; }

echo "--- unattached public repo (nix-community/nix-on-droid) ---"
probe "https://github.com/nix-community/nix-on-droid/archive/$REV.tar.gz"
probe "https://codeload.github.com/nix-community/nix-on-droid/tar.gz/$REV"
probe "https://api.github.com/repos/nix-community/nix-on-droid/tarball/$REV"
probe "https://api.github.com/repos/nix-community/nix-on-droid/commits/master"
probe "https://github.com/nix-community/nix-on-droid/info/refs?service=git-upload-pack"

echo "--- attached repo (agentydragon/ducktape) ---"
probe "https://codeload.github.com/agentydragon/ducktape/tar.gz/refs/heads/devel"
probe "https://api.github.com/repos/agentydragon/ducktape/commits/devel"

echo "--- nixpkgs (large, very commonly fetched) ---"
probe "https://codeload.github.com/NixOS/nixpkgs/tar.gz/refs/heads/nixos-unstable"
