# hostexecd — the host-side daemon haku-console calls to run an approved command
# on this node under the *approving operator's own Authentik identity*. There is no
# standing credential and no bespoke host key: the console exchanges the operator's
# own token for a short-lived, single-use, per-host token, and hostexecd verifies it
# against Authentik's JWKS before dropping to the token's `run_as` user. Trust model
# and daemon internals: haku/hostexec/PLAN.md and haku/hostexec/hostexecd/main.rs.
#
# Runs as root (it setuids to the token's `run_as`, which may be root or an
# unprivileged user), so this unit deliberately applies NO systemd sandboxing /
# capability restriction — that would defeat the per-call privilege drop.
#
# Network exposure: the socket binds to this node's Nebula mesh IP (from
# nebula-mesh.json), which for these workers is also the kubelet InternalIP — so the
# haku-console pod reaches it over the cluster pod network (nebula1/Cilium VXLAN),
# while the physical/roaming NIC never has a listener. This is the *only* network
# restriction achievable here: hostexecd is a host systemd daemon in the host
# netns (identity `reserved:host`), NOT a Cilium pod endpoint, so a
# CiliumNetworkPolicy cannot gate it, and Cilium's host firewall is off on these
# Talos/Nebula nodes. The k8s-worker firewall already trusts nebula1/cilium_host/lxc+
# and nothing else, so no per-port rule is needed and the port stays off the physical
# NIC. The load-bearing per-caller gate is the Authentik token verification; the bind
# address is coarse defense-in-depth.
{
  config,
  lib,
  hostexecd,
  ...
}:
let
  cfg = config.ducktape.hostexec;
  meshConfig = builtins.fromJSON (builtins.readFile ../../../nebula-mesh.json);
  nebulaIp = meshConfig.hosts.${cfg.host}.nebula_ip;
  # Authentik per-provider issuer (issuer_mode = "per_provider"); the token's `iss`
  # claim and JWKS live under the provider's application slug `hostexec-<host>`. Kept
  # in lockstep with tf/gitops/agent-machine-access/hostexec.tf.
  issuer = "https://${cfg.authDomain}/application/o/hostexec-${cfg.host}/";
in
{
  options.ducktape.hostexec = {
    enable = lib.mkEnableOption "hostexecd — verified remote command execution for haku-console";

    host = lib.mkOption {
      type = lib.types.str;
      default = config.networking.hostName;
      description = ''
        This host's short name. The token audience is `hostexec-<host>` and the
        required Authentik group is `hostexec-<run_as>-<host>`. Must match a key in
        nebula-mesh.json and the per-host provider in
        tf/gitops/agent-machine-access/hostexec.tf.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8765;
      description = "TCP port hostexecd listens on (bound to this node's Nebula IP).";
    };

    authDomain = lib.mkOption {
      type = lib.types.str;
      default = "auth.allegedly.works";
      description = "Authentik domain that issues the per-host hostexec tokens and publishes their JWKS.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = meshConfig.hosts ? ${cfg.host};
        message = "ducktape.hostexec: host '${cfg.host}' is not in nebula-mesh.json";
      }
    ];

    systemd.services.hostexecd = {
      description = "hostexecd — verified per-host remote command execution for haku-console";
      # Bind needs nebula1 up with the mesh IP assigned; Nebula assigns it
      # asynchronously after the unit starts, so Restart=on-failure retries the bind
      # until the address appears (a few cycles at boot / after a roaming reconnect).
      after = [
        "network-online.target"
        "nebula.service"
      ];
      wants = [
        "network-online.target"
        "nebula.service"
      ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "simple";
        ExecStart = lib.getExe hostexecd;
        Restart = "on-failure";
        RestartSec = "5";
        Environment = [
          "HOSTEXEC_HOST=${cfg.host}"
          "HOSTEXEC_ISSUER=${issuer}"
          "HOSTEXEC_JWKS_URL=${issuer}jwks/"
          "HOSTEXEC_BIND=${nebulaIp}:${toString cfg.port}"
          "RUST_LOG=info"
          # systemd's DefaultEnvironment PATH is a NixOS-minimal set (coreutils,
          # findutils, grep, sed, systemd) with no /run/current-system/sw/bin, so a bare
          # argv[0] like `hostname` (net-tools) can't be resolved at exec. Exec with the
          # system PATH so operator commands resolve as they would in a login shell.
          "PATH=/run/wrappers/bin:/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin"
        ];
      };
    };
  };
}
