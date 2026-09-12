{
  pkgs,
  nix-openclaw,
  ducktapePkgs,
}:

let
  # The gateway package, its source pin, and the npm-wrapper splice are shared
  # with haku/openclaw_spike; see ./gateway.nix.
  openclawGateway = import ./gateway.nix { inherit pkgs nix-openclaw; };
  inherit (openclawGateway) openclawPackages gateway;
  matrixPlugin = openclawPackages.openclawRuntimePlugins.matrix;
  # Brave is an official external runtime plugin. Bundle its pinned Nix artifact
  # with the gateway rather than installing it mutably in the state PVC.
  bravePlugin = openclawPackages.openclawRuntimePlugins.brave;
  # Matrix uses the plugin-state store for sync and encryption state. OpenClaw
  # grants that capability only to trusted plugins, and an arbitrary
  # plugins.load.paths entry is intentionally untrusted even when it points at
  # the official nix-openclaw derivation. Physically add the runtime package to
  # the gateway's bundled extension tree instead: discovery then records it as
  # origin=bundled and the normal state-store trust boundary remains intact.
  #
  # Keep both packaged roots complete: this release contains source-checkout
  # markers and therefore prefers dist/extensions, while a future package that
  # omits those markers will prefer dist-runtime/extensions. Hard-linking the
  # second tree avoids storing a duplicate copy of the plugin payload.
  gatewayWithRuntimePlugins = gateway.overrideAttrs (previous: {
    # nix-openclaw supplies a complete custom installPhase rather than the
    # stdenv default, so append here instead of relying on a postInstall hook
    # that the package's install script does not run.
    installPhase =
      previous.installPhase
      + "\n"
      + ''
        ${pkgs.bash}/bin/bash ${./bundle-runtime-plugin.sh} \
          "$out/lib/openclaw" matrix ${matrixPlugin}
        ${pkgs.bash}/bin/bash ${./bundle-runtime-plugin.sh} \
          "$out/lib/openclaw" brave ${bravePlugin}
      '';
  });

  # Keep the shell/coreutils surface needed by public-coder-agent's init
  # container, plus a deliberately compact set of tools repeatedly needed for
  # public-repository and GitOps work. The image is not the full devshell:
  # heavyweight, infrequently-used tooling such as checkov stays available via
  # the repository's development shell.
  #
  # `gh` normally reads GH_TOKEN/GITHUB_TOKEN. OpenClaw's generic exec
  # environment filter treats those names as credentials, with a current
  # local-Gateway exception for a native GitHub identity. GH_PAT is the
  # proxy-substituted, non-secret credential contract for this agent, so expose
  # a compatible `gh` wrapper rather than relying on that exception.
  ghWithProxyToken = pkgs.writeShellScriptBin "gh" ''
    export GH_TOKEN="''${GH_PAT:?GH_PAT is required for GitHub CLI authentication}"
    exec ${pkgs.gh}/bin/gh "$@"
  '';

  tools =
    with pkgs;
    [
      bashInteractive
      busybox
      buildifier
      cacert
      coreutils
      curl
      file
      git
      ghWithProxyToken
      jq
      kubeconform
      kubectl
      kubernetes-helm
      markdownlint-cli2
      nixfmt
      nodejs_22
      # `ssh`/`scp` for the devbox route through sshpiper
      # (cluster/k8s/agents/public-coder-agent/sshpiper). git pulls openssh into its own closure
      # but does not put a client on PATH.
      openssh
      pre-commit
      python3
      ruff
      ripgrep
      shfmt
      sops
      statix
      tflint
      tini
    ]
    ++ [
      # Local pre-commit hooks call these entry points. The package wraps its own compatible
      # Python + pygit2 closure, rather than depending on a persisted pip venv from an older image.
      ducktapePkgs.ducktape-git-hooks
      # The Nix package carries prettier-plugin-svelte and wraps NODE_PATH so the repository's
      # .prettierrc.cjs resolves reliably inside the minimal image.
      ducktapePkgs.prettier
    ];

  # The preload imports undici. Put it beside the Nix gateway's node_modules so
  # Node's ESM resolver finds the dependency exactly as it did in /app in the
  # Docker-built image. The symlink keeps the dependency closure shared.
  proxySetup = pkgs.runCommand "openclaw-proxy-setup" { } ''
    mkdir -p "$out/lib/openclaw"
    cp ${./proxy-setup.mjs} "$out/lib/openclaw/proxy-setup.mjs"
    ln -s ${gatewayWithRuntimePlugins}/lib/openclaw/node_modules "$out/lib/openclaw/node_modules"
  '';

  path = pkgs.lib.makeBinPath ([ gatewayWithRuntimePlugins ] ++ tools);
in
pkgs.dockerTools.buildLayeredImage {
  name = "ghcr.io/agentydragon/openclaw";
  # CI supplies the sortable devel-* tag selected by Flux.
  tag = null;

  contents = [
    gatewayWithRuntimePlugins
    proxySetup
  ]
  ++ tools;
  maxLayers = 100;

  fakeRootCommands = ''
    mkdir -p home/openclaw tmp etc/ssl/certs usr/bin
    chmod 1777 tmp
    chown -R 1000:1000 home/openclaw

    cat > etc/passwd <<'PASSWD'
    root:x:0:0:root:/root:/bin/sh
    openclaw:x:1000:1000:OpenClaw:/home/openclaw:/bin/sh
    nobody:x:65534:65534:Nobody:/:/bin/false
    PASSWD
    cat > etc/group <<'GROUP'
    root:x:0:
    openclaw:x:1000:
    nogroup:x:65534:
    GROUP
    cat > etc/nsswitch.conf <<'NSS'
    passwd: files
    group: files
    hosts: files dns
    NSS

    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt
    # pre-commit generates Bash hooks beginning `#!/usr/bin/env bash`. Nix packages use absolute
    # store shebangs, but generated project hooks do not, so retain this tiny FHS compatibility
    # link in the otherwise minimal image.
    ln -sf ${pkgs.coreutils}/bin/env usr/bin/env
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/home/openclaw";
    Env = [
      "PATH=${path}"
      "HOME=/home/openclaw"
      "USER=openclaw"
      "NODE_ENV=production"
      # --report-on-signal makes a wedged gateway diagnosable: `kill -USR2 1` writes
      # a diagnostic report (JS stack, native stack per thread, libuv handles) to
      # the diagnostics volume. Node generates it from a dedicated thread, which is the point --
      # public-coder hung with its main thread blocked in synchronous node:sqlite
      # work, so the event loop never turned, SIGUSR1 never opened the inspector,
      # and /proc/<pid>/{syscall,stack} were refused by PodSecurity baseline.
      # There was no way to ask the process what it was doing.
      #
      # Deliberately not --inspect: these containers execute agent-authored
      # commands, and an always-listening inspector on loopback would let any of
      # them attach to the gateway process and read its credentials.
      #
      # --report-on-fatalerror covers what --report-on-signal cannot. A heap-limit
      # abort has nobody present to send SIGUSR2, and the SIGABRT otherwise leaves
      # no report at all -- only the container exit code 134.
      #
      # --max-old-space-size is set rather than derived. Node sizes V8's old space
      # at about half of uv_get_constrained_memory(), which inside a container is
      # the cgroup memory limit -- so left implicit, `limits.memory` silently
      # decides the heap, and editing the limit for an unrelated reason moves the
      # heap with it. 2048 is what the current 4Gi limit already derives -- both
      # give heap_size_limit 2096 MiB -- so this changes nothing today and only
      # stops the heap drifting when the limit next moves. Keep it in step with
      # `limits.memory` in
      # cluster/k8s/agents/public-coder-agent/app/deployment.yaml.
      #
      # --heapsnapshot-signal is the on-demand trigger: `kill -s PWR 1` writes a
      # snapshot without the process having to be near death. Two taken an hour
      # apart diff into what is accumulating, which is the question a single
      # snapshot cannot answer. SIGPWR because SIGUSR2 is already the report
      # signal above and SIGUSR1 is Node's inspector, which the previous
      # paragraph rules out.
      #
      # --heapsnapshot-near-heap-limit=1 covers the unattended case, capturing
      # the state at the abort itself. Note it is the riskier of the two: V8
      # raises its own limit to serialize, so the spike has to fit inside
      # `limits.memory` or the kernel turns a clean abort into an OOMKill.
      #
      # Both directories are /diag, the dedicated claim mounted by
      # cluster/k8s/agents/public-coder-agent/app/deployment.yaml -- not /tmp,
      # whose emptyDir the next Flux roll discards along with the capture, and
      # not the state PVC, which volsync backs up. That mount is a precondition,
      # not a preference: without it Node writes a roughly heap-sized snapshot
      # into the container layer.
      "NODE_OPTIONS=--import=file://${proxySetup}/lib/openclaw/proxy-setup.mjs --report-on-signal --report-directory=/diag --report-on-fatalerror --heapsnapshot-signal=SIGPWR --heapsnapshot-near-heap-limit=1 --diagnostic-dir=/diag --max-old-space-size=2048"
      "NPM_CONFIG_PREFIX=/home/openclaw/.local"
      "NPM_CONFIG_CACHE=/home/openclaw/.cache/npm"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
    ];
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
    Entrypoint = [
      "${pkgs.tini}/bin/tini"
      "-s"
      "--"
    ];
    Cmd = [
      "${gatewayWithRuntimePlugins}/bin/openclaw"
      "gateway"
    ];
  };
}
