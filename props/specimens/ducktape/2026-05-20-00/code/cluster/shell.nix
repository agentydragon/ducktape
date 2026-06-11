{
  pkgs ? import <nixpkgs> {
    config.allowUnfreePredicate = pkg: builtins.elem (pkg.pname or "") [ "packer" ];
  },
}:
pkgs.mkShell {
  buildInputs = [
    pkgs.talosctl
    pkgs.hcloud # Hetzner Cloud CLI
    pkgs.packer # Packer for building Hetzner Talos snapshots (BSL license)
    pkgs.awscli2 # AWS CLI for Route 53 management
    pkgs.kyverno # Policy engine CLI (validate manifests, test policies)
    pkgs.nebula # Nebula mesh overlay (nebula-cert for PKI management)
    pkgs.ovhcloud-cli # OVH API CLI (Kimsufi server inventory, boot, IPMI)
    pkgs.python313Packages.ovh # OVH Python client for ad-hoc API scripts
  ];
}
