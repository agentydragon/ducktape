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

    httpsProxy = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        HTTP(S) proxy hostexecd's outbound reqwest client should use to reach `consoleUrl`/the
        JWKS endpoint, for a host whose egress is fenced through one (public-coder-devbox). Unset
        on every host that reaches the console directly (wyrm2/rugged/atlas).
      '';
    };

    extraRootCertFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Extra PEM root certificate file hostexecd should trust in addition to the built-in web
        roots. Needed only behind a TLS-*intercepting* egress proxy, whose leaf certificate for
        `consoleUrl` is signed by a local CA the built-in roots don't know.
      '';
    };

    daemonTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Path to the plaintext per-node routing bearer, bypassing this module's own
        `sops.secrets.hostexecd_daemon_token` declaration entirely. For a host with no
        Kubernetes relationship to the cluster (wyrm2/rugged/atlas), decrypting the committed
        `node-daemon-<host>.sops.yaml` locally via sops-nix is the only channel available, so
        leave this unset. A KubeVirt-managed guest (public-coder-devbox) has a simpler one:
        Flux/kustomize-controller already decrypts that same file server-side to materialize
        haku-console's own copy of the token, and KubeVirt can attach a second Secret carrying
        the identical value as a guest disk — no local sops/age identity needed on the guest at
        all. Set this to wherever that disk is installed.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets.hostexecd_daemon_token = lib.mkIf (cfg.daemonTokenFile == null) {
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
          "HOSTEXEC_DAEMON_TOKEN_FILE=${
            if cfg.daemonTokenFile != null then
              cfg.daemonTokenFile
            else
              config.sops.secrets.hostexecd_daemon_token.path
          }"
          "RUST_LOG=info"
          # systemd's DefaultEnvironment PATH is a NixOS-minimal set (coreutils,
          # findutils, grep, sed, systemd) with no /run/current-system/sw/bin, so a bare
          # argv[0] like `hostname` (net-tools) can't be resolved at exec. Exec with the
          # system PATH so operator commands resolve as they would in a login shell.
          "PATH=/run/wrappers/bin:/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin"
        ]
        ++ lib.optionals (cfg.httpsProxy != null) [
          "HTTP_PROXY=${cfg.httpsProxy}"
          "HTTPS_PROXY=${cfg.httpsProxy}"
          "NO_PROXY=127.0.0.1,localhost"
        ]
        ++ lib.optionals (cfg.extraRootCertFile != null) [
          # SSL_CERT_FILE, not a hostexec-specific name: it's the same variable this host already
          # sets for every other TLS client (see extraRootCertFile's own doc comment above for why
          # main.rs still has to read it and call add_root_certificate() explicitly rather than
          # relying on reqwest to honor it the way OpenSSL-based tools do).
          "SSL_CERT_FILE=${cfg.extraRootCertFile}"
        ];
      };
    };
  };
}
