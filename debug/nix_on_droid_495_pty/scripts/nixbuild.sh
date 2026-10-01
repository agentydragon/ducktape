#!/bin/sh
# Run inside nix-on-droid's proot. Forces a *local* Nix build: the pty code in
# DerivationBuilderImpl::startBuilder()/openSlave() runs once per local build
# and never for a path fetched from a binary cache, so a cache hit would not
# exercise issue #495 at all. Substituters are therefore emptied.
NIX=/nix/store/x603vg66akk9hzc6dcrjpjk2hj4mci8s-nix-2.20.5/bin/nix
BASH=/nix/store/226mxicx2n7hgdf2424vz5y526nlyly8-bash-5.2-p15/bin/bash

echo "== nix version =="
$NIX --version

echo "== building a trivial derivation locally =="
$NIX build --impure --no-link --print-out-paths \
  --extra-experimental-features nix-command \
  --option substituters '' \
  --option builders '' \
  --expr "derivation {
    name = \"pty-probe\";
    system = \"x86_64-linux\";
    builder = \"$BASH\";
    args = [ \"-c\" \"echo hello-from-a-local-build > \$out\" ];
  }"
echo "== build rc=$? =="
