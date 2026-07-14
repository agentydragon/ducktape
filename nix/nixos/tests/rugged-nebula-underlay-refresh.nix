# Test the Rugged-only Nebula underlay route refresh wiring.
#
# Run: nix eval --impure --file nix/nixos/tests/rugged-nebula-underlay-refresh.nix

let
  flake = builtins.getFlake "path:${toString ../../..}";
  rugged = flake.nixosConfigurations.rugged.config;
  wyrm2 = flake.nixosConfigurations.wyrm2.config;
  timerName = "nebula-underlay-route-refresh";
in
{
  test_rugged_has_route_refresh_timer = {
    expr = builtins.hasAttr timerName rugged.systemd.timers;
    expected = true;
  };

  test_wyrm2_has_no_route_refresh_timer = {
    expr = builtins.hasAttr timerName wyrm2.systemd.timers;
    expected = false;
  };

  test_route_refresh_is_debounced_for_five_seconds = {
    expr = rugged.systemd.timers.${timerName}.timerConfig.OnActiveSec;
    expected = "5s";
  };

  test_route_refresh_timer_is_not_started_at_boot = {
    expr = rugged.systemd.timers.${timerName}.wantedBy;
    expected = [ ];
  };

  test_rugged_nebula_listener_is_dual_stack = {
    expr =
      (builtins.fromJSON (builtins.readFile rugged.environment.etc."nebula/config.yaml".source))
      .listen.host;
    expected = "::";
  };

  test_wyrm2_nebula_listener_is_dual_stack = {
    expr =
      (builtins.fromJSON (builtins.readFile wyrm2.environment.etc."nebula/config.yaml".source))
      .listen.host;
    expected = "::";
  };

  test_nebula_config_change_restarts_worker_stack = {
    expr =
      let
        configFile = rugged.environment.etc."nebula/config.yaml".source;
      in
      builtins.all (service: builtins.elem configFile rugged.systemd.services.${service}.restartTriggers)
        [
          "nebula"
          "haproxy"
          "kubelet"
        ];
    expected = true;
  };
}
