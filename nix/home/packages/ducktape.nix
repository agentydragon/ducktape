# ducktape: CLI tools (git-commit-ai, difftree, gmail-archiver)
# Wheel fetched as a flake input (ducktape-wheel) from GitHub Releases.
# To update: nix flake lock --update-input ducktape-wheel ./nix
{
  lib,
  pkgs,
  ducktape-wheel,
}:
let
  compact-json = pkgs.callPackage ./compact-json.nix { };
in
pkgs.python3Packages.buildPythonApplication {
  pname = "ducktape";
  version = "latest";
  format = "wheel";

  src = ducktape-wheel;

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

    # gmail_archiver deps
    beautifulsoup4
    # email-reply-parser not in nixpkgs — lazily imported
    google-api-python-client
    google-auth-httplib2
    google-auth-oauthlib
    pydantic-settings
    python-dateutil
    pyyaml

    # Not in nixpkgs - from overlay
    compact-json
  ];

  # Disable checks - wheel is tested in CI
  doCheck = false;

  meta = {
    description = "CLI tools (git-commit-ai, difftree, gmail-archiver)";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "git-commit-ai";
  };
}
