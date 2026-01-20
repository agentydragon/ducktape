{
  pkgs ? import <nixpkgs> { },
}:
let
  # Pin to nixpkgs-unstable for latest kubeseal
  unstable =
    import (fetchTarball "https://github.com/NixOS/nixpkgs/archive/nixpkgs-unstable.tar.gz")
      { };
in
pkgs.mkShell {
  buildInputs = [
    pkgs.talosctl
    pkgs.fluxcd
    pkgs.kubernetes-helm
    pkgs.kustomize # For kustomize build validation
    pkgs.kubeconform # For Kubernetes manifest validation
    pkgs.nodePackages.prettier # For YAML formatting
    pkgs.tflint
    pkgs.hcloud # Hetzner Cloud CLI
    pkgs.yq-go # YAML/JSON conversion tool
    pkgs.popeye # Kubernetes cluster health checker
    pkgs.python3 # For health-check.py
    pkgs.python3Packages.rich # Rich UI for health-check.py
    pkgs.python3Packages.aiohttp # HTTP client for API tests
    pkgs.python3Packages.kubernetes # Kubernetes API client
    # Use kubeseal from unstable to get v0.32.2
    unstable.kubeseal
  ];
}
