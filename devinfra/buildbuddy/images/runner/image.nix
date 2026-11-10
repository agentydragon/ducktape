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

  archive = pkgs.dockerTools.buildLayeredImage {
    name = "buildbuddy-remote-runner";
    tag = "latest";
    fromImage = baseImageArchive;
    contents = [ runnerToolLinks ];
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
