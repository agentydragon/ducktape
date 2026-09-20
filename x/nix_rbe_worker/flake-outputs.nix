{
  nixpkgs,
  self,
  system,
}:
{
  nixosConfigurations.nix-rbe-worker = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [ ./nixos.nix ];
  };

  packages.${system}.nix-rbe-nixos =
    self.nixosConfigurations.nix-rbe-worker.config.system.build.tarball;
}
