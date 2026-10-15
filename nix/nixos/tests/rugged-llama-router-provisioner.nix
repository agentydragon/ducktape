# Build and run the router artifact provisioner against a localhost fixture
# using the real pinned llama.cpp downloader, without external network access.
#
# Run: nix build --file nix/nixos/tests/rugged-llama-router-provisioner.nix --no-link --print-out-paths

let
  flake = builtins.getFlake "path:${toString ../../..}";
  pkgs = flake.nixosConfigurations.rugged.pkgs;
  provisioner = pkgs.writeShellApplication {
    name = "provision-rugged-llama-router-models";
    runtimeInputs = [
      flake.packages.${pkgs.stdenv.hostPlatform.system}.llama-router
      pkgs.coreutils
      pkgs.jq
    ];
    text = builtins.readFile ../hosts/rugged/provision_llama_router_models.sh;
  };
in
pkgs.runCommand "rugged-llama-router-provisioner-test"
  {
    nativeBuildInputs = [
      provisioner
      pkgs.python3
    ];
  }
  ''
    python3 ${./test_rugged_llama_router_provisioner.py} \
      ${provisioner}/bin/provision-rugged-llama-router-models
    touch "$out"
  ''
