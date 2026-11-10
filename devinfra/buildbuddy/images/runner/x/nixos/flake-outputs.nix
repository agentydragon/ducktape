{
  nixpkgs,
  self,
  system,
}:
{
  nixosConfigurations.buildbuddy-remote-runner = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [ ./nixos.nix ];
  };

  packages.${system}.buildbuddy-remote-runner-nixos-image =
    self.nixosConfigurations.buildbuddy-remote-runner.config.system.build.tarball;
}
