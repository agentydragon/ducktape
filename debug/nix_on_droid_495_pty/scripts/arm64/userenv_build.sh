#!/bin/sh
# Run inside nix-on-droid's proot. Builds a user-environment derivation -- the
# exact derivation named in issue #495 -- from the nixpkgs channel on
# nixos.org, which this session's egress policy does serve.
#
# This is NOT the literal `nix-on-droid switch --flake ...` command and does not
# substitute for it: that command is blocked here because its flake inputs are
# GitHub tarballs the proxy refuses. What this does establish is whether the
# pty path in DerivationBuilderImpl::openSlave() fails on Android 14 arm64 when
# a real user-environment is built locally by the app.
NIX=$(command -v nix-env || echo /nix/var/nix/profiles/default/bin/nix-env)
echo "== nix-env: $NIX"
"$NIX" --version

echo "== adding the nixpkgs channel (nixos.org is reachable; github tarballs are not)"
nix-channel --add https://nixos.org/channels/nixos-24.05 nixpkgs
nix-channel --update nixpkgs

echo "== installing hello, which builds a user-environment.drv locally"
"$NIX" -iA nixpkgs.hello
echo "== rc=$?"
