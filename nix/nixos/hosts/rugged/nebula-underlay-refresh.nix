{ pkgs, ... }:
let
  stateDirectory = "nebula-underlay-route-refresh";
  stateFile = "/var/lib/${stateDirectory}/default-routes.json";

  routeSnapshot = pkgs.writeShellScript "nebula-underlay-route-snapshot" ''
    set -euo pipefail

    printf '{"ipv4":'
    ${pkgs.iproute2}/bin/ip -4 -json route show table main default \
      | ${pkgs.jq}/bin/jq -cS 'map(del(.expires)) | sort_by(tojson)'
    printf ',"ipv6":'
    ${pkgs.iproute2}/bin/ip -6 -json route show table main default \
      | ${pkgs.jq}/bin/jq -cS 'map(del(.expires)) | sort_by(tojson)'
    printf '}\n'
  '';

  seedRouteState = pkgs.writeShellScript "nebula-underlay-route-state-seed" ''
    set -euo pipefail

    candidate="$(${pkgs.coreutils}/bin/mktemp ${stateFile}.XXXXXX)"
    trap '${pkgs.coreutils}/bin/rm -f "$candidate"' EXIT
    ${routeSnapshot} > "$candidate"
    ${pkgs.coreutils}/bin/mv "$candidate" ${stateFile}
    trap - EXIT
  '';

  refreshNebula = pkgs.writeShellScript "nebula-underlay-route-refresh" ''
    set -euo pipefail

    candidate="$(${pkgs.coreutils}/bin/mktemp ${stateFile}.XXXXXX)"
    trap '${pkgs.coreutils}/bin/rm -f "$candidate"' EXIT
    ${routeSnapshot} > "$candidate"

    # A Wi-Fi-to-WWAN handoff can briefly leave both families without a
    # default route. Preserve the last usable state and let the later link-up
    # event perform one refresh after the replacement route has settled.
    if ! ${pkgs.jq}/bin/jq -e '([.ipv4[], .ipv6[]] | length) > 0' "$candidate" > /dev/null; then
      exit 0
    fi

    if ${pkgs.diffutils}/bin/cmp --silent ${stateFile} "$candidate"; then
      exit 0
    fi

    echo "IPv4 or IPv6 default routes changed; restarting the Nebula worker stack"
    ${pkgs.systemd}/bin/systemctl --job-mode=replace restart \
      nebula.service haproxy.service kubelet.service
    ${pkgs.coreutils}/bin/mv "$candidate" ${stateFile}
    trap - EXIT
  '';
in
{
  # These are the post-change actions that can alter an interface's default
  # routes. Global connectivity events carry no interface name, while
  # pre-up/pre-down fire before the kernel route state has settled.
  networking.networkmanager.dispatcherScripts = [
    {
      source = pkgs.writeShellScript "nebula-underlay-route-dispatcher" ''
        case "$1:$2" in
          wlp0s20f3:up | wlp0s20f3:down | wlp0s20f3:dhcp4-change | wlp0s20f3:dhcp6-change | wlp0s20f3:reapply | \
            wwan0:up | wwan0:down | wwan0:dhcp4-change | wwan0:dhcp6-change | wwan0:reapply | \
            :connectivity-change)
            ${pkgs.systemd}/bin/systemctl restart nebula-underlay-route-refresh.timer
            ;;
        esac
      '';
      type = "basic";
    }
  ];

  # Seed the baseline before Nebula starts so boot does not look like an
  # underlay change. RemainAfterExit lets the refresh service require the seed
  # without recapturing the baseline on every dispatcher event.
  systemd.services.nebula-underlay-route-state-seed = {
    description = "Seed the Nebula underlay default-route state";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    before = [ "nebula.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      StateDirectory = stateDirectory;
      StateDirectoryMode = "0700";
      ExecStart = seedRouteState;
    };
  };

  systemd.services.nebula-underlay-route-refresh = {
    description = "Refresh the Nebula worker stack after an underlay route change";
    requires = [ "nebula-underlay-route-state-seed.service" ];
    after = [ "nebula-underlay-route-state-seed.service" ];
    serviceConfig = {
      Type = "oneshot";
      StateDirectory = stateDirectory;
      StateDirectoryMode = "0700";
      ExecStart = refreshNebula;
    };
  };

  # The dispatcher restarts this otherwise-idle timer. Repeated route events
  # therefore move the one-shot refresh five seconds into the future.
  systemd.timers.nebula-underlay-route-refresh = {
    description = "Debounce Nebula underlay route changes";
    timerConfig = {
      OnActiveSec = "5s";
      AccuracySec = "1s";
      Unit = "nebula-underlay-route-refresh.service";
    };
  };
}
