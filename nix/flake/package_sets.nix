# Package sets, CI-released artifacts and the devShell, shared by the root
# flake and the devshell flake (nix/flake.nix). The devshell flake is a `path:`
# flake rooted at nix/, so nothing reachable from here may read outside nix/.
{
  nixpkgs,
  nixpkgs-unstable,
  fenix,
  system,
}:
let
  codexAwsLcOverlay = _final: prev: {
    codex = prev.codex.overrideAttrs (old: {
      # aws-lc-sys otherwise probes OpenSSL first and rejects its headers.
      # AWS-LC's headers and libraries live in separate Nix outputs, and
      # its headers are nested under include/aws-lc.
      # The unstable Codex recipe also lacks the system libraries demanded
      # by Nix's cargo hook; master already declares most of these.
      buildInputs = old.buildInputs ++ [
        prev.aws-lc
        prev.oniguruma
        prev.sqlite
        prev.zstd.dev
      ];
      env = old.env // {
        AWS_LC_SYS_SYSTEM_DIR = prev.symlinkJoin {
          name = "codex-aws-lc-system";
          paths = [
            prev.aws-lc.out
            prev.aws-lc.dev
          ];
          postBuild = ''
            ln -s aws-lc/openssl "$out/include/openssl"
          '';
        };
      };
    });
  };
  pkgs = import nixpkgs {
    inherit system;
    config.allowUnfree = true;
  };
  pkgsUnstable = import nixpkgs-unstable {
    inherit system;
    config.allowUnfree = true;
    overlays = [ codexAwsLcOverlay ];
  };

  artifactData = builtins.fromJSON (builtins.readFile ../artifact-pins.json);
  rawArtifactOverrides = builtins.getEnv "DUCKTAPE_ARTIFACT_OVERRIDES";
  artifactOverrides =
    if rawArtifactOverrides == "" then { } else builtins.fromJSON rawArtifactOverrides;
  # PR overrides may include new artifacts that have not been published and
  # pinned yet. Keep their original filename from the local build output; for
  # published artifacts, use the pin URL's basename as usual.
  artifactSpecs =
    artifactData.pins
    // builtins.mapAttrs (
      name: path: (artifactData.pins.${name} or { }) // { overridePath = path; }
    ) artifactOverrides;

  # CI-released artifact pins (nix/artifact-pins.json), updated by sync-pins.yml.
  # Use Nixpkgs fetchurl derivations, not builtins.fetchurl: hosts behind
  # restricted egress can substitute these fixed-output paths from Attic
  # instead of doing evaluator-time downloads from GitHub Releases.
  #
  # PR-time overrides may replace published artifacts or supply new ones
  # before release.yml publishes them and sync-pins.yml creates their pins.
  # nix-wheel-check.yml supplies artifacts built from this PR. Requires
  # --impure (getEnv).
  # Empty in normal use; behaviour is identical to the pre-override flake.
  artifacts = builtins.mapAttrs (
    name: spec:
    if spec ? overridePath then
      # Preserve the local artifact's basename. Wheel installers validate
      # the archive filename against its embedded .dist-info directory.
      builtins.path {
        path = /. + spec.overridePath;
        name = baseNameOf spec.overridePath;
      }
    else
      pkgs.fetchurl {
        inherit (spec) url;
        name =
          let
            asset = baseNameOf spec.url;
          in
          if pkgs.lib.hasSuffix ".whl" asset || pkgs.lib.hasSuffix ".zip" asset then asset else "source";
        hash = "sha256-${spec.sha256}";
      }
  ) artifactSpecs;

  # Keep the developer Ruff binary aligned with the repository's pinned
  # Python and Bazel toolchains while nixpkgs catches up.
  ruffLatest = pkgs.stdenvNoCC.mkDerivation {
    pname = "ruff";
    version = "0.17.4";
    src = pkgs.fetchurl {
      url = "https://github.com/astral-sh/ruff/releases/download/0.17.4/ruff-x86_64-unknown-linux-gnu.tar.gz";
      hash = "sha256-OrKzSurNvB6lxSnNvaPgV5JjPVhyKBI/0J87Wg+ssno=";
    };
    nativeBuildInputs = [ pkgs.autoPatchelfHook ];
    buildInputs = [ pkgs.stdenv.cc.cc.lib ];
    dontBuild = true;
    dontConfigure = true;
    dontCheck = true;
    installPhase = ''
      install -Dm755 ruff $out/bin/ruff
    '';
    meta.mainProgram = "ruff";
  };

  ducktapePkgs = import ../packages {
    inherit (pkgs) lib;
    inherit
      pkgs
      pkgsUnstable
      artifacts
      ;
  };
  devTools = import ./devtools.nix {
    inherit
      pkgs
      pkgsUnstable
      ducktapePkgs
      ruffLatest
      ;
    rustToolchain = fenix.packages.${system}.stable;
  };
in
{
  inherit
    codexAwsLcOverlay
    pkgs
    pkgsUnstable
    artifacts
    ruffLatest
    ducktapePkgs
    devTools
    ;
  devShell = pkgs.mkShell {
    # Keep each Python CLI's dependencies in its own wrapper, not the shared shell.
    dontAddPythonPath = "1";
    packages = devTools.devToolPackages ++ devTools.localOnlyPackages ++ devTools.systemLibs.packages;
    inherit (devTools.systemLibs) buildInputs;
    LD_LIBRARY_PATH = devTools.systemLibs.libraryPath;
  };
}
