# Test the Rugged-only RAPL counter permission wiring.
#
# Run: nix eval --impure --file nix/nixos/tests/rugged-rapl-metrics.nix

let
  flake = builtins.getFlake "path:${toString ../../..}";
  rugged = flake.nixosConfigurations.rugged.config;
  wyrm2 = flake.nixosConfigurations.wyrm2.config;
  gid = 45321;
in
{
  test_rugged_owns_dedicated_rapl_reader_group = {
    expr = rugged.users.groups.node-exporter-rapl.gid;
    expected = gid;
  };

  test_rugged_reapplies_rapl_permissions_on_powercap_rebind = {
    expr = builtins.match ".*SYSTEMD_WANTS.*rapl-energy-permissions.*" rugged.services.udev.extraRules;
    expected = [ ];
  };

  test_rugged_rapl_service_keeps_only_needed_capabilities = {
    expr = rugged.systemd.services.rapl-energy-permissions.serviceConfig.CapabilityBoundingSet;
    expected = [
      "CAP_CHOWN"
      "CAP_FOWNER"
    ];
  };

  test_wyrm2_has_no_rapl_permission_service = {
    expr = builtins.hasAttr "rapl-energy-permissions" wyrm2.systemd.services;
    expected = false;
  };
}
