# headscale-cleanup: CLI tool for cleaning up stale Headscale nodes
# Installed from CI-built wheel via GitHub Releases
#
# To update: change shortSha to new 8-char commit SHA, set hash to lib.fakeHash,
# run home-manager switch to get the new hash, then update hash.
{
  lib,
  pkgs,
}: let
  # 8-char commit SHA from GitHub release tag
  shortSha = "023784bf";

  # Fetch wheel directly with fetchurl
  wheelSrc = pkgs.fetchurl {
    url = "https://github.com/agentydragon/ducktape/releases/download/headscale-cleanup-${shortSha}/headscale_cleanup-0.1.0-py3-none-any.whl";
    # After updating shortSha, set to lib.fakeHash and rebuild to get new hash
    hash = "sha256-Wcz5SA/6yW1RM/uWQwvrk4e39GCVpY0kgusc6mUcuAc=";
  };
in
  pkgs.python3Packages.buildPythonApplication {
    pname = "headscale-cleanup";
    version = "latest";
    format = "wheel";

    src = wheelSrc;

    propagatedBuildInputs = with pkgs.python3Packages; [
      typer
    ];

    # Disable checks - wheel is tested in CI
    doCheck = false;

    meta = {
      description = "CLI tool for cleaning up stale Headscale nodes";
      homepage = "https://github.com/agentydragon/ducktape";
      license = lib.licenses.agpl3Only;
      mainProgram = "headscale-cleanup";
    };
  }
