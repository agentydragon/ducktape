# Adapter exposing gaffer-private-built artifacts as Nix packages without
# fetching gaffer-private source. The pin file `nix/gaffer-pins.json` is
# updated by gaffer-private's CI after a successful push to
# cache.allegedly.works/gaffer; consumers reach the closures via
# `builtins.fetchClosure` against that cache (per-host reader JWTs).
#
# Empty pins (initial state, before gaffer CI's first push) → empty attrset.
# Populated pins → store paths fetched via `builtins.fetchClosure`. Unlike
# `builtins.storePath` (which is forbidden in pure flake eval),
# `fetchClosure` is pure-eval-friendly: it declares a hermetic dependency
# on a substituter URL + store path. Cache signature is still checked
# against the daemon's trusted-public-keys.
_:
let
  inherit ((builtins.fromJSON (builtins.readFile ../gaffer-pins.json))) pins;
  fetch =
    _: spec:
    builtins.fetchClosure {
      fromStore = "https://cache.allegedly.works/gaffer";
      fromPath = spec.store_path;
      # The closure is input-addressed (regular nix-build output, not CA);
      # the daemon verifies the cache's signature against trusted-public-keys
      # (`gaffer:Z8sM…` per nix/nixos/modules/attic-substituter.nix).
      inputAddressed = true;
    };
in
builtins.mapAttrs fetch pins
