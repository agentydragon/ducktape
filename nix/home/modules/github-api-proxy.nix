# Transport-only relay to the central GitHub-observation proxy. Interception,
# capture and mitigation policy belong to the central service.
# `claude-proxied` opts a CLI session in; desktopPackage covers the normal Desktop
# command, launcher actions and URI callbacks without changing the app profile.
{
  config,
  lib,
  pkgs,
  ducktapePackages,
  ...
}:
let
  cfg = config.ducktape.githubApiProxy;
  stateDir = "${config.xdg.stateHome}/github-api-proxy";
  caCert = "${cfg.remote.caCertificate}";
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
    enable = lib.mkEnableOption "transport-only relay to the central GitHub API proxy";

    port = lib.mkOption {
      type = lib.types.port;
      default = 8788;
      description = "Loopback relay port.";
    };

    remote = {
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
        assertion = !(lib.hasPrefix builtins.storeDir cfg.remote.credentialsFile);
        message = "GitHub proxy credentials must be a runtime secret outside the Nix store.";
      }
    ];
    systemd.user.services.github-api-proxy = {
      Unit.Description = "Transport relay to the central GitHub API proxy";
      Install.WantedBy = [ "default.target" ];
      Service = {
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
    ];
  };
}
