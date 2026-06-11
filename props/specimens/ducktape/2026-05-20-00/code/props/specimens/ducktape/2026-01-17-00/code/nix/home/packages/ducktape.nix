# ducktape: CLI tools collection (git-commit-ai, difftree)
# Installed from CI-built wheel via GitHub Releases
#
# To update: change shortSha to new 8-char commit SHA, set hash to lib.fakeHash,
# run home-manager switch to get the new hash, then update hash.
{
  lib,
  pkgs,
}: let
  compact-json = pkgs.callPackage ./compact-json.nix {};

  # 8-char commit SHA from GitHub release tag
  shortSha = "24303c05";

  # Wheel filename follows PEP 427: {name}-{version}-{python}-{abi}-{platform}.whl
  wheelFilename = "ducktape-0.1.0-py3-none-any.whl";

  # Fetch wheel directly with fetchurl
  wheelSrc = pkgs.fetchurl {
    url = "https://github.com/agentydragon/ducktape/releases/download/ducktape-${shortSha}/${wheelFilename}";
    # After updating shortSha, set to lib.fakeHash and rebuild to get new hash
    hash = "sha256-BDf/l1yCLEqH4cH3NrbDmID8A27WXC4exuMGeAYGTBM=";
  };

  ducktape = pkgs.python3Packages.buildPythonApplication {
    pname = "ducktape";
    version = "latest";
    format = "wheel";

    src = wheelSrc;

    propagatedBuildInputs = with pkgs.python3Packages; [
      # git-commit-ai deps
      aiodocker
      anyio
      httpx
      jinja2
      mako
      openai
      pydantic
      pygit2
      rich
      structlog
      tenacity
      typer

      # MCP dependencies
      fastmcp
      mcp

      # Testing dependencies (used at runtime for matchers)
      pyhamcrest

      # difftree deps
      click
      unidiff

      # Not in nixpkgs - from overlay
      compact-json
    ];

    # Disable checks - wheel is tested in CI
    doCheck = false;

    meta = {
      description = "CLI tools collection: git-commit-ai, difftree";
      homepage = "https://github.com/agentydragon/ducktape";
      license = lib.licenses.agpl3Only;
      mainProgram = "git-commit-ai";
    };
  };
in
  ducktape
