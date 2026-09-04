# Request-level visibility into what Claude Code and Claude Desktop send to the
# GitHub API.
#
# The connection recorder (<../../nixos/modules/github-api-recorder.nix>) counts
# TCP connections, which cannot distinguish a process that opens one socket and
# sends three requests from one that opens one socket and sends three thousand.
# Both products keep HTTP/2 connections alive, and both have been observed
# spending this account's GraphQL budget, so connection counts have twice pointed
# at the wrong culprit. See <debug/github_rate_limit_monitoring_blind_spot.md>.
#
# CLEANUP(added 2026-09-04): remove once that note names the consumers and the
# upstream issues are resolved.
#
# Deliberately not wired into the normal `claude` and `claude-desktop`: this
# provides `claude-proxied` and `claude-desktop-proxied` instead. Routing a daily
# driver through a proxy means a stopped proxy breaks it, and neither app is worth
# that for a diagnostic. Run the wrapper for a measurement session, read the flows,
# go back to the normal binary.
#
# Only api.github.com is decrypted. Everything else — the Anthropic API above all —
# is blind-tunnelled by `--allow-hosts`, so conversations never pass through a MITM
# to debug GitHub calls.
#
# The point is to exonerate as much as to attribute. GraphQL responses carry
# `data.rateLimit.cost`, and the exporter records the account-wide `used` delta over
# the same window. Summing the first and subtracting from the second gives the
# residual: points spent by something that did not pass through this proxy. A large
# residual says the consumer is not the client being measured — which matters,
# because every attribution in the investigation so far has come from correlating
# spikes against whatever processes happened to be running, and two of them were
# wrong. A proxied client that spends 40 points while the account loses 5000 has
# been cleared, and that is a result worth as much as a positive.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.githubApiProxy;
  confDir = "${config.xdg.configHome}/github-api-proxy";
  stateDir = "${config.xdg.stateHome}/github-api-proxy";
  caCert = "${confDir}/mitmproxy-ca-cert.pem";
  proxyUrl = "http://127.0.0.1:${toString cfg.port}";

  # NODE_EXTRA_CA_CERTS covers the Node/undici side (Claude Code, and Electron's
  # main process); --proxy-server plus --ignore-certificate-errors covers
  # Chromium's own network stack, which ignores both env vars above.
  proxiedEnv = ''
    export HTTPS_PROXY=${proxyUrl} HTTP_PROXY=${proxyUrl}
    export NODE_EXTRA_CA_CERTS=${caCert}
    if [ ! -f ${caCert} ]; then
      echo "github-api-proxy: no CA at ${caCert} — is the proxy service running?" >&2
      echo "  systemctl --user status github-api-proxy" >&2
      exit 1
    fi
  '';
in
{
  options.ducktape.githubApiProxy = {
    enable = lib.mkEnableOption "local intercepting proxy for GitHub API request accounting";

    port = lib.mkOption {
      type = lib.types.port;
      default = 8788;
      description = "Loopback port for the intercepting proxy.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.github-api-proxy = {
      Unit.Description = "Intercepting proxy for GitHub API request accounting";
      Install.WantedBy = [ "default.target" ];
      Service = {
        Type = "simple";
        ExecStartPre = "${pkgs.coreutils}/bin/mkdir -p ${stateDir} ${confDir}";
        # --allow-hosts is an allowlist of what to *decrypt*; everything else is
        # tunnelled without inspection. Flows land in a file rather than stdout so
        # `mitmdump -nr` can replay and aggregate them afterwards.
        ExecStart = lib.escapeShellArgs [
          "${pkgs.mitmproxy}/bin/mitmdump"
          "--listen-host"
          "127.0.0.1"
          "--listen-port"
          (toString cfg.port)
          "--set"
          "confdir=${confDir}"
          "--allow-hosts"
          "^api\\.github\\.com:443$"
          "-w"
          "${stateDir}/github.flows"
          "--set"
          "flow_detail=0"
        ];
        Restart = "on-failure";
        RestartSec = 10;
      };
    };

    home.packages = [
      (pkgs.writeShellScriptBin "claude-proxied" ''
        ${proxiedEnv}
        exec ${lib.getExe config.programs.claude-code.package} "$@"
      '')

      (pkgs.writeShellScriptBin "claude-desktop-proxied" ''
        ${proxiedEnv}
        exec claude-desktop \
          --proxy-server=127.0.0.1:${toString cfg.port} \
          --ignore-certificate-errors "$@"
      '')

      # Summarise a capture: request count and total GraphQL cost per client.
      (pkgs.writeShellScriptBin "github-api-proxy-report" ''
        exec ${pkgs.mitmproxy}/bin/mitmdump -nr ${stateDir}/github.flows \
          --set flow_detail=1 "$@"
      '')
    ];
  };
}
