{
  devtools,
  pkgs,
  nix-openclaw,
  ducktapePkgs,
}:

let
  system = pkgs.stdenv.hostPlatform.system;
  gateway = nix-openclaw.packages.${system}.openclaw-gateway;
  matrixPlugin = nix-openclaw.packages.${system}.openclaw-runtime-plugin-matrix;
  # Brave is an official external runtime plugin. Bundle its pinned Nix artifact
  # with the gateway rather than installing it mutably in the state PVC.
  bravePlugin = nix-openclaw.packages.${system}.openclaw-runtime-plugin-brave;
  repoBazelVersion = pkgs.lib.removeSuffix "\n" (builtins.readFile ../.bazelversion);
  # The primary nixpkgs pin still has Bazel 8.4.2. Override it with the
  # repository's 8.6.0 pin and the corresponding upstream dist hash rather
  # than weakening the version invariant or moving the whole package set.
  bazelPkg = (pkgs.bazel_8.override { version = repoBazelVersion; }).overrideAttrs {
    src = pkgs.fetchzip {
      url = "https://github.com/bazelbuild/bazel/releases/download/${repoBazelVersion}/bazel-${repoBazelVersion}-dist.zip";
      hash = "sha256-W22eB0IzHNZe3xaF8AZOkUTDCic3NXkypdqSDY61Su0=";
      stripRoot = false;
    };
  };
  # wrapProgram keeps the patched executable behind this shell wrapper. Bazelisk
  # symlinks local binaries into its cache and preserves that symlink as argv[0];
  # the shell wrapper's exec -a then makes Bazel resolve itself from the cache
  # path and fail with LOCAL_ENVIRONMENTAL_ERROR. Point Bazelisk at the wrapped
  # ELF itself instead.
  bazelExecutable = "${bazelPkg}/bin/.bazel-${repoBazelVersion}-linux-x86_64-wrapped";

  # Bazelisk's own Go binary works in the minimal Nix image, but the upstream
  # Bazel ELF it downloads does not: it requests the absent FHS loader at
  # /lib64/ld-linux-x86-64.so.2. Install nixpkgs' patched Bazel as `bazel`.
  # BuildBuddy's CLI embeds Bazelisk for local argument canonicalization; the
  # BB_USE_BAZEL_VERSION environment variable below makes that embedded
  # Bazelisk use the actual wrapped ELF rather than downloading another one.
  # Neither nixpkgs' `bin/bazel` version selector nor the versioned makeWrapper
  # script is safe to invoke through Bazelisk's local-binary cache symlink.

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
  # `gh` normally reads GH_TOKEN/GITHUB_TOKEN, but OpenClaw deliberately strips
  # those names from executed commands. GH_PAT is the proxy-substituted,
  # non-secret credential contract for this agent, so expose a compatible `gh`
  # wrapper rather than requiring every call site to re-export it.
  ghWithProxyToken = pkgs.writeShellScriptBin "gh" ''
    export GH_TOKEN="''${GH_PAT:?GH_PAT is required for GitHub CLI authentication}"
    exec ${pkgs.gh}/bin/gh "$@"
  '';

  tools =
    with pkgs;
    [
      bashInteractive
      busybox
      bazelPkg
      buildifier
      cacert
      coreutils
      curl
      file
      git
      ghWithProxyToken
      jq
      jdk_headless
      kubeconform
      kubectl
      kubernetes-helm
      markdownlint-cli2
      nixfmt
      nodejs_22
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
      # The agent does its work in this container, so give it the existing
      # declaratively-built `.#devtools` toolchain. In
      # particular bbr and its pygit2 extension stay in the Nix closure that
      # matches this image's Python, rather than relying on a persisted pip
      # installation from a previous image.
      devtools
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
assert bazelPkg.version == repoBazelVersion;
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
      # BuildBuddy invokes Bazel with --ignore_all_rc_files while loading flag
      # metadata, bypassing nixpkgs' system bazelrc and its --server_javabase.
      # Keep the matching Nix JDK explicit so the wrapped Bazel ELF can start.
      "JAVA_HOME=${pkgs.jdk_headless}"
      "NODE_ENV=production"
      "NODE_OPTIONS=--import=file://${proxySetup}/lib/openclaw/proxy-setup.mjs"
      "NPM_CONFIG_PREFIX=/home/openclaw/.local"
      "NPM_CONFIG_CACHE=/home/openclaw/.cache/npm"
      "BB_USE_BAZEL_VERSION=${bazelExecutable}"
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
