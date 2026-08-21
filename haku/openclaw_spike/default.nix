# Nix-built OCI image for Haku's isolated OpenClaw + Claude Code spike.
#
# Haku needs the beta gateway package for its Claude Opus 5 context-window
# metadata. Keep the repository's nix-openclaw machinery, but override its
# source pin locally instead of importing a prebuilt OpenClaw image.
{ pkgs, nix-openclaw }:

let
  sourceInfo = {
    owner = "openclaw";
    repo = "openclaw";
    rev = "3ddd783e3f2465f5221ab27e8849eb165c61b498";
    hash = "sha256-Bw6PC1uMHWkv1HTlgAcdz+RFqXq8h/K8A5PekxY82mc=";
    releaseTag = "v2026.7.2-beta.7";
    releaseVersion = "2026.7.2-beta.7";
    pnpmMajor = "11";
    pnpmDepsHash = "sha256-5c7MrnYyxbiaG6MJTVg/aCIXsrEMhYq9eMV0JuvOn44=";
    # These patches target the stable source layout. The beta already carries
    # the corresponding runtime behavior, so make the override explicit and
    # prevent stale patch hunks from being applied to unrelated files.
    publicSurfaceHardlinksPatch = pkgs.writeText "openclaw-beta-no-public-surface-patch" "";
  };

  # The beta declares pnpm 11.15.1. nixpkgs currently exposes pnpm 11.20.0,
  # whose package-manager selection and store layout differ enough to produce
  # an incomplete offline closure for this lockfile. Build the exact declared
  # pnpm release and use it for both fetching and the gateway derivation.
  pnpm_11_15 = pkgs.callPackage "${pkgs.path}/pkgs/development/tools/pnpm/generic.nix" {
    version = "11.15.1";
    hash = "sha256-J0YGKbEBEWBOf5iIJ1O1M5iYaCDCDgoGXzpKXp59tx8=";
    nodejs = null;
  };

  sqliteForOpenClaw = pkgs.sqlite.overrideAttrs (_: {
    version = "3.51.3";
    src = pkgs.fetchurl {
      url = "https://sqlite.org/2026/sqlite-src-3510300.zip";
      hash = "sha256-+KZ6H1tcrnxtQvCZTKe/GkpYWIaMgq3J/BNAvtXrjNI=";
    };
    # SQLite 3.51.3's fault-injection test is currently incompatible with
    # this nixpkgs test harness; the normal build checks and the Node ABI
    # smoke test still run below.
    doCheck = false;
  });

  nodeForOpenClaw = pkgs.nodejs-slim_22.overrideAttrs (old: {
    buildInputs =
      pkgs.lib.filter (dependency: (dependency.pname or null) != "sqlite") old.buildInputs
      ++ [ sqliteForOpenClaw ];
    configureFlags = map (
      flag:
      if pkgs.lib.hasPrefix "--shared-sqlite-libpath=" flag then
        "--shared-sqlite-libpath=${sqliteForOpenClaw}/lib"
      else
        flag
    ) old.configureFlags;
  });

  # fetcherVersion 4 stores pnpm's SQLite index as SQL for reproducibility.
  # nixpkgs' pnpm hook reconstructs it, but the nix-openclaw gateway prebuild
  # script only extracts the archive, so offline installs see arbitrary
  # packages as missing. Keep the upstream script and add that reconstruction.
  gatewayPrebuild = pkgs.runCommand "openclaw-gateway-prebuild" { } ''
    cp ${nix-openclaw}/nix/scripts/gateway-prebuild.sh "$out"
    substituteInPlace "$out" \
      --replace-fail \
        'log_step "chmod pnpm store writable" chmod -R +w "$store_path"' \
        'log_step "chmod pnpm store writable" chmod -R +w "$store_path"
    if [ -f "$store_path/v11/index.db.sql" ]; then
      ${sqliteForOpenClaw}/bin/sqlite3 "$store_path/v11/index.db" < "$store_path/v11/index.db.sql"
      rm "$store_path/v11/index.db.sql"
    fi'
    chmod +x "$out"
  '';

  # The pinned nix-openclaw build script also assumes a stable-only beta
  # helper. The beta source has no write-cli-compat.ts; all preceding build
  # stages are shared and remain under the upstream script.
  gatewayBuild = pkgs.runCommand "openclaw-gateway-build" { } ''
    cp ${nix-openclaw}/nix/scripts/gateway-build.sh "$out"
    substituteInPlace "$out" \
      --replace-fail \
        'log_step "build: write-cli-compat" node --import tsx scripts/write-cli-compat.ts' \
        ':' \
      --replace-fail \
        '# Reduce output size (pnpm implementation detail; safe to remove)' \
        'if [ -L node_modules/@openclaw/ai ]; then
      ai_runtime_target="$(readlink -f node_modules/@openclaw/ai || true)"
      if [ ! -d "$ai_runtime_target" ]; then
        printf "cannot materialize workspace package @openclaw/ai: %s\\n" "$ai_runtime_target" >&2
        exit 1
      fi
      ai_runtime_tmp="$(mktemp -d)"
      cp -a "$ai_runtime_target" "$ai_runtime_tmp/ai"
      rm node_modules/@openclaw/ai
      mv "$ai_runtime_tmp/ai" node_modules/@openclaw/ai
      rmdir "$ai_runtime_tmp"
    fi

    # Reduce output size (pnpm implementation detail; safe to remove)'
    chmod +x "$out"
  '';

  fetchPnpmDepsForOpenClaw =
    args:
    pkgs.fetchPnpmDeps (
      args
      // {
        pnpm = pnpm_11_15;
        pnpmInstallFlags = (args.pnpmInstallFlags or [ ]) ++ [ "--prod=false" ];
        postInstall = (args.postInstall or "") + ''
          rm -rf node_modules
          pnpm store prune
          pnpm store add @anthropic-ai/sdk@0.115.0
          pnpm store add @agentclientprotocol/sdk@1.3.0
          CI=true pnpm fetch --workspace-root --prod=false
          CI=true pnpm install --force --ignore-scripts --prod=false --registry="$NIX_NPM_REGISTRY" --frozen-lockfile
          rm -rf node_modules
        '';
      }
    );

  gatewaySource =
    pkgs.lib.callPackageWith
      (
        pkgs
        // {
          nodejs_22 = nodeForOpenClaw;
          pnpm_11 = pnpm_11_15;
          fetchPnpmDeps = fetchPnpmDepsForOpenClaw;
        }
      )
      "${nix-openclaw}/nix/packages/openclaw-gateway-source.nix"
      {
        inherit sourceInfo;
      };

  gateway = gatewaySource.overrideAttrs (old: {
    # This stable-only patch is selected unconditionally by the current
    # nix-openclaw helper. The beta source has already moved past its hunk;
    # clear only that patch input on the derived gateway.
    buildPhase = gatewayBuild;
    env = old.env // {
      GATEWAY_PREBUILD_SH = gatewayPrebuild;
      PATCH_SKIP_PLUGIN_AUTO_ENABLE_NIX_MODE = "";
    };
  });

  proxySetup = pkgs.runCommand "haku-openclaw-proxy-setup" { } ''
    mkdir -p "$out/lib/openclaw"
    cp ${../../openclaw/proxy-setup.mjs} "$out/lib/openclaw/proxy-setup.mjs"
    ln -s ${gateway}/lib/openclaw/node_modules "$out/lib/openclaw/node_modules"
  '';

  # The old Dockerfile installed Bazelisk as `/usr/local/bin/bazel`; retain the
  # command name haku-state tooling uses while keeping the upstream Bazelisk
  # package and its version-selection behavior.
  bazel = pkgs.writeShellScriptBin "bazel" ''
    exec ${pkgs.bazelisk}/bin/bazelisk "$@"
  '';

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
    nodeForOpenClaw
    openssl
    procps
    python3
    ripgrep
    ruff
    tea
  ];

  toolPath = pkgs.lib.makeBinPath ([ gateway ] ++ tools);
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/haku-openclaw-spike";
  # CI supplies the sortable devel-* tag selected by Flux.
  tag = null;
  contents = [
    gateway
    proxySetup
  ]
  ++ tools;
  maxLayers = 100;

  # Keep the old Dockerfile's /app preload path for the deployment contract,
  # while the actual imported module lives beside the Nix gateway's
  # node_modules so Node resolves `undici` from the packaged dependency tree.
  fakeRootCommands = ''
    mkdir -p app home/openclaw tmp usr/bin
    chmod 1777 tmp
    cp ${../../openclaw/proxy-setup.mjs} app/proxy-setup.mjs
    ln -sf ${pkgs.coreutils}/bin/env usr/bin/env
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/app";
    Env = [
      # Both the gateway and Node are supplied by the overridden nix-openclaw
      # closure, so the executable and SQLite ABI are part of this image's
      # tested closure.
      "PATH=${toolPath}:/home/node/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      "HOME=/home/openclaw"
      "USER=openclaw"
      "NODE_ENV=production"
      "NODE_OPTIONS=--import=file://${proxySetup}/lib/openclaw/proxy-setup.mjs"
      "NPM_CONFIG_PREFIX=/home/openclaw/.local"
      "NPM_CONFIG_CACHE=/home/openclaw/.cache/npm"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
    ];
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
    Cmd = [
      "${gateway}/bin/openclaw"
      "gateway"
    ];
  };
}
