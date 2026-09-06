# Request-level visibility into what Claude Code and Claude Desktop send to the
# GitHub API.
#
# The connection recorder (<../../nixos/modules/github-api-recorder.nix>) counts
# TCP connections, which cannot distinguish a process that opens one socket and
# sends three requests from one that opens one socket and sends three thousand.
# Both products keep HTTP/2 connections alive; connection counts alone cannot
# establish either one's contribution to the shared account quota.
#
# CLEANUP(added 2026-09-04): remove once that note names the consumers and the
# upstream issues are resolved.
#
# `claude-proxied` opts a CLI session into capture. Hosts may select desktopPackage
# to route the normal Desktop command, launcher actions, and URI callbacks through
# the proxy while retaining the normal app profile.
#
# In local mode everything is decrypted, deliberately. Earlier versions decrypted only
# api.github.com; that narrowness blinded this instrument and the connection
# recorder in the same place at the same time, and hours of "nothing else touches
# the API" turned out to mean "nothing else touches the four addresses we happened
# to list". Operator's call, on the operator's own machine: capture everything and
# filter at analysis time. Remote mode only transports traffic to the central
# interceptor and never writes a local raw capture. In local mode, Anthropic API traffic from a proxied
# session passes through the MITM and lands in the flow file. Always-proxied hosts
# accumulate this sensitive raw capture continuously, without size/age rotation.
#
# Request records preserve explicit GraphQL cost when the response includes it.
# Missing cost is unknown. The account-wide `x-ratelimit-used` header is not a
# request cost; differences also include concurrent callers and possible penalties.
{
  config,
  lib,
  pkgs,
  ducktapePackages,
  ...
}:
let
  cfg = config.ducktape.githubApiProxy;
  confDir = "${config.xdg.configHome}/github-api-proxy";
  stateDir = "${config.xdg.stateHome}/github-api-proxy";
  captureFile = "${stateDir}/github.flows";
  caCert =
    if cfg.remote.enable then "${cfg.remote.caCertificate}" else "${confDir}/mitmproxy-ca-cert.pem";
  proxyUrl = "http://127.0.0.1:${toString cfg.port}";
  desktopNssDir = "${stateDir}/claude-desktop/nssdb";
  rawDesktop = ducktapePackages.claude-desktop;

  caBundle = "${stateDir}/ca-bundle.pem";
  relayConfig = "%t/github-api-relay/squid.conf";
  prepareTrust = pkgs.writeShellApplication {
    name = "github-api-proxy-prepare-trust";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.nssTools
    ];
    text = builtins.readFile ../../../devinfra/github_api_capture/prepare_trust.sh;
  };

  # Every runtime in a session has to trust the interception CA, not just Node.
  # A session burns GraphQL through both its own Node client and `gh` subprocesses,
  # and Go reads neither NODE_EXTRA_CA_CERTS nor the Nix cert path — so without
  # SSL_CERT_FILE, `gh` fails TLS, makes no API call, and spends no quota. The
  # proxy would then *suppress* the behaviour under measurement and report a false
  # negative. Same reasoning for Python (REQUESTS_CA_BUNDLE) and git (GIT_SSL_CAINFO).
  #
  # The bundle is the system store plus the mitm CA: pointing SSL_CERT_FILE at the
  # mitm CA alone works for intercepted hosts but breaks TLS to every host the proxy
  # tunnels rather than decrypts.
  proxiedEnv = nssDir: ''
    ${lib.getExe prepareTrust} ${
      lib.escapeShellArgs (
        [
          caCert
          "/etc/ssl/certs/ca-bundle.crt"
          stateDir
        ]
        ++ lib.optional (nssDir != null) nssDir
      )
    }
    export HTTPS_PROXY=${proxyUrl} HTTP_PROXY=${proxyUrl}
    export NODE_EXTRA_CA_CERTS=${caCert}
    export SSL_CERT_FILE=${caBundle} REQUESTS_CA_BUNDLE=${caBundle} GIT_SSL_CAINFO=${caBundle}
  '';

  # GhRestClient uses Electron net.fetch, requiring Chromium routing and trust.
  # Chromium's own NSS store is isolated below; Node CA variables do not cover it.
  desktopLauncher = pkgs.writeShellScriptBin "claude-desktop-proxied" ''
    set -euo pipefail
    umask 077
    ${pkgs.coreutils}/bin/mkdir -p ${stateDir}
    ${pkgs.systemd}/bin/systemctl --user start github-api-proxy.service
    # In remote mode this traverses authenticated upstream TLS, not just the
    # loopback listener. mitm.it is served centrally without an account request.
    ${pkgs.curl}/bin/curl --silent --show-error --fail --output /dev/null \
      --proxy ${proxyUrl} --noproxy "" --retry 5 --retry-connrefused \
      --retry-delay 1 --max-time 2 http://mitm.it/
    ${proxiedEnv desktopNssDir}
    # Chromium prioritizes ~/.pki/nssdb even with --user-data-dir. This mount
    # is visible only to the diagnostic app and its descendants; global
    # Chromium trust and HOME remain unchanged. /dev must retain device access.
    exec ${pkgs.bubblewrap}/bin/bwrap \
      --bind / / --dev-bind /dev /dev \
      --tmpfs ${config.home.homeDirectory}/.pki \
      --bind ${desktopNssDir} ${config.home.homeDirectory}/.pki/nssdb \
      -- ${lib.getExe rawDesktop} "$@" --proxy-server=${proxyUrl}
  '';

  desktopPackage = pkgs.symlinkJoin {
    name = "claude-desktop-github-proxy-${rawDesktop.version}";
    paths = [ rawDesktop ];
    postBuild = ''
      rm "$out/bin/claude-desktop"
      ln -s ${lib.getExe desktopLauncher} "$out/bin/claude-desktop"
      ln -s ${lib.getExe desktopLauncher} "$out/bin/claude-desktop-proxied"
      rm "$out/share/applications/com.anthropic.Claude.desktop"
      substitute ${rawDesktop}/share/applications/com.anthropic.Claude.desktop \
        "$out/share/applications/com.anthropic.Claude.desktop" \
        --replace-fail ${lib.getExe rawDesktop} "$out/bin/claude-desktop"
    '';
    inherit (rawDesktop) meta;
  };
in
{
  options.ducktape.githubApiProxy = {
    enable = lib.mkEnableOption "local intercepting proxy for GitHub API request accounting";

    blockCloudGithubBatch = lib.mkEnableOption "temporary mitigation for cloud GitHub batch polling fan-out";

    port = lib.mkOption {
      type = lib.types.port;
      default = 8788;
      description = "Loopback port shared by local capture and remote relay modes.";
    };

    remote = {
      enable = lib.mkEnableOption "a transport-only relay to the central HTTPS forward proxy";
      host = lib.mkOption {
        type = lib.types.strMatching "[A-Za-z0-9.-]+";
        description = "HTTPS parent hostname, verified against its TLS certificate.";
      };
      port = lib.mkOption {
        type = lib.types.port;
        default = 443;
        description = "HTTPS parent port.";
      };
      credentialsFile = lib.mkOption {
        type = lib.types.str;
        description = "Owner-only runtime JSON file containing one username to 64-character lowercase hex password mapping; use a decrypted SOPS path, never a Nix path literal.";
      };
      caCertificate = lib.mkOption {
        type = lib.types.path;
        description = "Verified, pinned public central interception CA PEM, trusted only by the proxied app. Never a private signing key.";
      };
    };

    desktopPackage = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      default = desktopPackage;
      description = "Claude Desktop with all normal launch routes using this proxy and private NSS trust.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !(cfg.remote.enable && cfg.blockCloudGithubBatch);
        message = "Remote GitHub proxy mode requires blockCloudGithubBatch=false; central policy owns interception.";
      }
      {
        assertion = !cfg.remote.enable || !(lib.hasPrefix builtins.storeDir cfg.remote.credentialsFile);
        message = "GitHub proxy credentials must be a runtime secret outside the Nix store.";
      }
    ];
    systemd.user.services.github-api-proxy = {
      Unit.Description =
        if cfg.remote.enable then
          "Transport relay to the central GitHub API proxy"
        else
          "Intercepting proxy for GitHub API request accounting";
      Install.WantedBy = [ "default.target" ];
      Service =
        if cfg.remote.enable then
          {
            Type = "simple";
            UMask = "0077";
            RuntimeDirectory = "github-api-relay";
            RuntimeDirectoryMode = "0700";
            ExecStartPre = lib.escapeShellArgs [
              "${pkgs.python3}/bin/python3"
              "${../../../devinfra/github_api_capture/relay_config.py}"
              "--host"
              cfg.remote.host
              "--port"
              (toString cfg.remote.port)
              "--listen-port"
              (toString cfg.port)
              "--credentials-file"
              cfg.remote.credentialsFile
              "--ca-bundle"
              "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "--output"
              relayConfig
            ];
            ExecStart = "${pkgs.squid}/bin/squid -N -f ${relayConfig}";
            # Squid parser/fatal diagnostics can echo its secret-bearing cache_peer.
            # Health is the authenticated launcher probe plus the unit exit status.
            StandardOutput = "null";
            StandardError = "null";
            LimitCORE = 0;
            NoNewPrivileges = true;
            Restart = "on-failure";
            RestartSec = 10;
          }
        else
          {
            Type = "simple";
            UMask = "0077";
            ExecStartPre = toString (
              pkgs.writeShellScript "github-api-proxy-prepare" ''
                set -euo pipefail
                ${pkgs.coreutils}/bin/install -d -m 0700 -- ${lib.escapeShellArg stateDir} ${lib.escapeShellArg confDir}
                # UMask does not restrict an existing capture.
                if [ -f ${lib.escapeShellArg captureFile} ]; then
                  ${pkgs.coreutils}/bin/chmod 0600 -- ${lib.escapeShellArg captureFile}
                fi
              ''
            );
            # No --allow-hosts: everything is decrypted. Flows land in a file rather
            # than stdout so `mitmdump -nr` can replay and aggregate them afterwards.
            ExecStart = lib.escapeShellArgs (
              [
                "${pkgs.mitmproxy}/bin/mitmdump"
                "--listen-host"
                "127.0.0.1"
                "--listen-port"
                (toString cfg.port)
                "--set"
                "confdir=${confDir}"
                "-w"
                # + preserves existing flows when the service restarts.
                "+${captureFile}"
                "--set"
                "flow_detail=0"
              ]
              ++ lib.optionals cfg.blockCloudGithubBatch [
                "-s"
                (toString ../../../devinfra/github_api_capture/block_cloud_github.py)
                "--set"
                "block_cloud_github_batch=true"
                "--set"
                "cloud_github_block_events=${stateDir}/cloud-github-block-events.jsonl"
              ]
            );
            Restart = "on-failure";
            RestartSec = 10;
          };
    };

    home.packages = [
      (pkgs.writeShellScriptBin "claude-proxied" ''
        set -euo pipefail
        umask 077
        ${proxiedEnv null}
        exec ${lib.getExe config.programs.claude-code.package} "$@"
      '')

      desktopLauncher
    ]
    ++ lib.optionals (!cfg.remote.enable) [
      # Offline JSONL metadata only; no request/response bodies or auth headers.
      (pkgs.writeShellScriptBin "github-api-proxy-report" ''
        exec ${pkgs.mitmproxy}/bin/mitmdump -q -nr ${lib.escapeShellArg captureFile} \
          -s ${../../../devinfra/github_api_capture/report.py} \
          -s ${../../../devinfra/github_api_capture/cloud_report.py} "$@"
      '')
    ];
  };
}
