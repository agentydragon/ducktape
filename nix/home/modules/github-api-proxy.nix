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
# Everything is decrypted, deliberately. Earlier versions decrypted only
# api.github.com; that narrowness blinded this instrument and the connection
# recorder in the same place at the same time, and hours of "nothing else touches
# the API" turned out to mean "nothing else touches the four addresses we happened
# to list". Operator's call, on the operator's own machine: capture everything and
# filter at analysis time. Note this does mean Anthropic API traffic from a proxied
# session passes through the MITM and lands in the flow file, so run the wrappers
# for measurement sessions rather than leaving them on.
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

  caBundle = "${stateDir}/ca-bundle.pem";

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
  proxiedEnv = ''
    if [ ! -f ${caCert} ]; then
      echo "github-api-proxy: no CA at ${caCert} — is the proxy service running?" >&2
      echo "  systemctl --user status github-api-proxy" >&2
      exit 1
    fi
    if [ ! -f ${caBundle} ] || [ ${caCert} -nt ${caBundle} ]; then
      cat "$(${pkgs.coreutils}/bin/readlink -f /etc/ssl/certs/ca-bundle.crt)" ${caCert} > ${caBundle}
    fi
    export HTTPS_PROXY=${proxyUrl} HTTP_PROXY=${proxyUrl}
    export NODE_EXTRA_CA_CERTS=${caCert}
    export SSL_CERT_FILE=${caBundle} REQUESTS_CA_BUNDLE=${caBundle} GIT_SSL_CAINFO=${caBundle}
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
        # No --allow-hosts: everything is decrypted. Flows land in a file rather
        # than stdout so `mitmdump -nr` can replay and aggregate them afterwards.
        ExecStart = lib.escapeShellArgs [
          "${pkgs.mitmproxy}/bin/mitmdump"
          "--listen-host"
          "127.0.0.1"
          "--listen-port"
          (toString cfg.port)
          "--set"
          "confdir=${confDir}"
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

      # Gotcha: Claude Desktop refuses to start if a Chromium network-override
      # switch is on its command line -- "refusing to start — a debugging or
      # network-override switch is present" -- so --proxy-server and
      # --ignore-certificate-errors are out. Only the environment is available.
      # That is very likely enough: GhRestClient logs to main.log, so it runs in
      # the main (Node) process, and the app's bundle references HTTPS_PROXY,
      # NO_PROXY, undici and ProxyAgent. Requests made from a renderer through
      # Chromium's own stack stay unproxied and unmeasured.
      (pkgs.writeShellScriptBin "claude-desktop-proxied" ''
        ${proxiedEnv}
        # Electron hands off to an existing instance and exits, so launching this
        # while an unproxied app is running silently measures nothing: the window
        # appears, the flows file stays empty. Refuse instead of lying.
        if ${pkgs.procps}/bin/pgrep -f 'claude-desktop/claude-desktop' >/dev/null; then
          echo "claude-desktop is already running; this would hand off to that" >&2
          echo "unproxied instance and capture nothing. Quit it first:" >&2
          echo "  pkill -f 'claude-desktop/claude-desktop'" >&2
          exit 1
        fi
        exec claude-desktop "$@"
      '')

      # Summarise a capture: request count and total GraphQL cost per client.
      (pkgs.writeShellScriptBin "github-api-proxy-report" ''
        exec ${pkgs.mitmproxy}/bin/mitmdump -nr ${stateDir}/github.flows \
          --set flow_detail=1 "$@"
      '')
    ];
  };
}
