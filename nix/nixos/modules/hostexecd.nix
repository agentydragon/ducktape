# hostexecd — outbound node daemon that claims approved commands from haku-console
# on this node under the *approving operator's own Authentik identity*. A per-node
# bearer authenticates heartbeat/claim traffic but grants no execution authority:
# every claimed command still carries a short-lived, single-use per-host operator
# token, which hostexecd verifies before dropping to its `run_as` user. Trust model
# and daemon internals: haku/hostexec/README.md and haku/hostexec/hostexecd/main.rs.
#
# Runs as root (it setuids to the token's `run_as`, which may be root or an
# unprivileged user), so this unit deliberately applies NO systemd sandboxing /
# capability restriction — that would defeat the per-call privilege drop.
#
# Network exposure: none. The daemon has no listener and uses outbound HTTPS to the
# public console origin, so roaming and off-mesh nodes need no inbound route or open
# host port.
{
  config,
  lib,
  hostexecd,
  ...
}:
let
  cfg = config.ducktape.hostexec;
  daemonSecretFile = ../../../cluster/k8s/haku/console + "/node-daemon-${cfg.host}.sops.yaml";
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
        the daemon id in haku-console's config and the per-host provider in
        tf/gitops/agent-machine-access/hostexec.tf.
      '';
    };

    authDomain = lib.mkOption {
      type = lib.types.str;
      default = "auth.allegedly.works";
      description = "Authentik domain that issues the per-host hostexec tokens and publishes their JWKS.";
    };

    consoleUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://haku.allegedly.works";
      description = "Public Haku Console origin hostexecd heartbeats and long-polls.";
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets.hostexecd_daemon_token = {
      sopsFile = daemonSecretFile;
      key = "stringData/token";
      owner = "root";
      mode = "0400";
    };

    systemd.services.hostexecd = {
      description = "hostexecd — verified per-host remote command execution for haku-console";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "simple";
        ExecStart = lib.getExe hostexecd;
        Restart = "always";
        RestartSec = "5";
        Environment = [
          "HOSTEXEC_HOST=${cfg.host}"
          "HOSTEXEC_ISSUER=${issuer}"
          "HOSTEXEC_JWKS_URL=${issuer}jwks/"
          "HOSTEXEC_CONSOLE_URL=${cfg.consoleUrl}"
          "HOSTEXEC_DAEMON_TOKEN_FILE=${config.sops.secrets.hostexecd_daemon_token.path}"
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
