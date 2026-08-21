# Nix-built OCI image for Haku's isolated OpenClaw + Claude Code spike.
#
# Keep the upstream beta as the base rather than reusing the public-coder
# gateway package: nix-openclaw is currently pinned to 2026.7.1-2, while this
# spike deliberately needs 2026.7.2-beta.7's Claude Opus 5 context-window
# metadata. Everything added by this repository is a Nix package, replacing
# the old apt/npm/curl Dockerfile provisioning with a reviewable closure.
{ pkgs }:

let
  # The old Dockerfile installed Bazelisk as `/usr/local/bin/bazel`; retain the
  # command name haku-state tooling uses while keeping the upstream Bazelisk
  # package and its version-selection behavior.
  bazel = pkgs.writeShellScriptBin "bazel" ''
    exec ${pkgs.bazelisk}/bin/bazelisk "$@"
  '';

  upstreamOpenClaw = pkgs.dockerTools.pullImage {
    imageName = "ghcr.io/openclaw/openclaw";
    # The tag resolves to a multi-platform OCI index. dockerTools selects the
    # host architecture while copying it; pinning the index prevents a mutable
    # tag from silently changing the deployed OpenClaw runtime.
    imageDigest = "sha256:d41807ff1e5c925ff75e71ed2b755cdea59da1431d1f4fde5051a16a3337e9ce";
    # Hash of dockerTools' skopeo-generated OCI archive for that immutable
    # index. It makes the upstream base a fixed Nix input as well.
    hash = "sha256-mtB/8YxZxg5qhVJIn5WSeMhSeSx/Vp+UpwrU6EHm5Ps=";
    finalImageName = "ghcr.io/openclaw/openclaw";
    finalImageTag = "2026.7.2-beta.7";
  };

  # Mirrors the old Dockerfile's runtime surface, but each tool is now pinned
  # by flake.lock / nixpkgs rather than an apt repository or an ad-hoc curl
  # download. Claude Code is the Nix package, so it also avoids an
  # image-build-time npm install.
  tools = with pkgs; [
    bashInteractive
    bazel
    binutils
    cacert
    claude-code
    coreutils
    curl
    gcc
    git
    gnumake
    jdk_headless
    jq
    kubectl
    less
    # The upstream CLI wrapper is `#!/usr/bin/env node`. dockerTools does not
    # expose the base image's Node executable after it writes this image's PATH,
    # so retain a compatible Node runtime explicitly rather than inheriting an
    # implicit base-layer executable.
    nodejs_22
    openssl
    procps
    python3
    ripgrep
    ruff
    tea
  ];

  toolPath = pkgs.lib.makeBinPath tools;
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/haku-openclaw-spike";
  # CI supplies the sortable devel-* tag selected by Flux.
  tag = null;
  fromImage = upstreamOpenClaw;
  contents = tools;
  maxLayers = 100;

  # The upstream CLI launcher uses `#!/usr/bin/env node`, but its otherwise
  # minimal beta base does not contain coreutils' `env`. The old Dockerfile
  # happened to add it through apt; make that FHS compatibility path explicit
  # now that coreutils comes from Nix. Put the proxy preload in the layer too:
  # it must be a real /app file, not a symlink into /nix/store, so Node resolves
  # `undici` from the upstream gateway's adjacent node_modules directory.
  fakeRootCommands = ''
    mkdir -p app usr/bin
    cp ${../../openclaw/proxy-setup.mjs} app/proxy-setup.mjs
    ln -sf ${pkgs.coreutils}/bin/env usr/bin/env
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/app";
    Env = [
      # Keep the upstream Node/OpenClaw bin directories after the Nix tools:
      # `openclaw` and `node openclaw.mjs gateway` remain supplied by the
      # pinned upstream beta, whereas all supporting CLI tools come from Nix.
      "PATH=${toolPath}:/home/node/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      "HOME=/home/openclaw"
      "USER=openclaw"
      "NODE_ENV=production"
      "NODE_OPTIONS=--import=file:///app/proxy-setup.mjs"
      "NPM_CONFIG_PREFIX=/home/openclaw/.local"
      "NPM_CONFIG_CACHE=/home/openclaw/.cache/npm"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
    ];
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
    Cmd = [
      "node"
      "openclaw.mjs"
      "gateway"
    ];
  };
}
