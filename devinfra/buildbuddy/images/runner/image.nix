# Deterministic Nix assembly for the BuildBuddy Remote Runner image.
#
# The workflow authenticates to GHCR outside the Nix build, pulls the
# digest-pinned base image into a Docker archive, and imports that archive into
# the Nix store. DUCKTAPE_RUNNER_BASE_IMAGE names that store path; it contains
# no registry credentials. Evaluation therefore uses --impure for this path,
# while the image derivation itself has only declared store inputs.
{
  pkgs,
  runnerTools,
}:
let
  baseImageArchivePath = builtins.getEnv "DUCKTAPE_RUNNER_BASE_IMAGE";
  baseImageArchive =
    if baseImageArchivePath == "" then null else builtins.storePath baseImageArchivePath;

  # Keep the tool closure in /nix/store and expose its commands on the PATH
  # expected by BuildBuddy's goinit runner. This leaves the base image's /bin
  # and /usr/bin contents intact.
  runnerToolLinks = pkgs.runCommand "buildbuddy-remote-runner-tool-links" { } ''
    mkdir -p "$out/usr/local/bin"
    for tool in ${runnerTools}/bin/*; do
      [ -x "$tool" ] || continue
      ln -s "$tool" "$out/usr/local/bin/$(basename "$tool")"
    done
  '';

  # Keep the profiler's injected allocator shim on the same Ubuntu glibc as
  # the unmodified RBE-built analyzer. Nixpkgs Heaptrack links its preload to
  # Nix glibc, which cannot be injected through Ubuntu's older loader.
  ubuntuDeb =
    section: name: hash:
    pkgs.fetchurl {
      url = "https://archive.ubuntu.com/ubuntu/pool/${section}/${name}_amd64.deb";
      inherit hash;
    };
  ubuntuHeaptrackDebs = [
    (ubuntuDeb "universe/h/heaptrack" "heaptrack_1.5.0+dfsg1-2ubuntu3"
      "sha256-JeCcFeIEwleLgaHoLROKefBGrSbBS7VLWzhbKHWMdFY="
    )
    (ubuntuDeb "universe/h/heaptrack" "libheaptrack_1.5.0+dfsg1-2ubuntu3"
      "sha256-FEO8rq9B9vm6E3HkBtSSjBPXbqcb0GiM/w1s2349bfg="
    )
    (ubuntuDeb "main/b/boost1.83" "libboost-filesystem1.83.0_1.83.0-2.1ubuntu3"
      "sha256-02MpE5BwA3zgiq1/Euy/atFK52S13YJJFf3H4xkgU2k="
    )
    (ubuntuDeb "main/b/boost1.83" "libboost-iostreams1.83.0_1.83.0-2.1ubuntu3"
      "sha256-SJIncNbfK8+CwqWQZCO7jmcZk2yKUFKUvOVka66Un0c="
    )
    (ubuntuDeb "main/b/boost1.83" "libboost-program-options1.83.0_1.83.0-2.1ubuntu3"
      "sha256-py9cJ9wG18xf/7vYGNcd7EMWhxrbGBU5TXFv0XwVAIg="
    )
  ];
  ubuntuHeaptrack =
    pkgs.runCommand "buildbuddy-remote-runner-ubuntu-heaptrack" { nativeBuildInputs = [ pkgs.dpkg ]; }
      ''
        mkdir -p "$out"
        for deb in ${pkgs.lib.escapeShellArgs ubuntuHeaptrackDebs}; do
          dpkg-deb --extract "$deb" "$out"
        done
      '';

  archive = pkgs.dockerTools.buildLayeredImage {
    name = "buildbuddy-remote-runner";
    tag = "latest";
    fromImage = baseImageArchive;
    contents = [
      runnerToolLinks
      ubuntuHeaptrack
    ];
    includeStorePaths = true;
    architecture = "amd64";
    created = "1970-01-01T00:00:01Z";
    mtime = "1970-01-01T00:00:01Z";

    # dockerTools carries the base Env forward but does not inherit the other
    # image config fields. Keep the observed runner command and provenance
    # labels explicit.
    config = {
      Cmd = [
        "/bin/sh"
        "-c"
        "bash"
      ];
      ArgsEscaped = true;
      Labels = {
        "org.opencontainers.image.ref.name" = "ubuntu";
        "org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
        "org.opencontainers.image.version" = "24.04";
      };
    };
  };
in
if baseImageArchive == null then
  pkgs.runCommand "buildbuddy-remote-runner-image-missing-base" { } ''
    echo "Set DUCKTAPE_RUNNER_BASE_IMAGE to the Nix store path returned by 'nix store add-file'" >&2
    echo "after authenticated, digest-pinned GHCR prefetch; build with nix build --impure." >&2
    exit 1
  ''
else
  # Include OCI serialization and its tooling in the derivation used by the
  # stale-pin check, not just the intermediate Docker archive.
  pkgs.runCommand "buildbuddy-remote-runner-oci"
    {
      nativeBuildInputs = [ pkgs.skopeo ];
      passthru = { inherit archive; };
    }
    ''
      skopeo --tmpdir "$TMPDIR" copy --insecure-policy --format oci --dest-compress-format gzip \
        docker-archive:${archive} oci:$out:runner
    ''
