# Ducktape wheel packages — built from CI-released wheels via flake inputs.
{
  lib,
  pkgs,
  ducktape-wheel,
  claude-hooks-wheel,
  gterm-theme-wheel,
  ducktape-util-wheel,
}:
let
  # Flake inputs with flake=false produce store paths named "source" (no .whl
  # extension). pypaInstallPhase globs *.whl, so we rename to restore it.
  renameWheel =
    name: input:
    pkgs.runCommand name { } ''
      cp ${input} $out
    '';

  mkDucktapeWheel =
    {
      pname,
      wheel,
      wheelFilename,
      description,
      propagatedBuildInputs ? [ ],
      nativeBuildInputs ? [ ],
      buildInputs ? [ ],
      mainProgram ? null,
    }:
    pkgs.python3Packages.buildPythonApplication {
      inherit pname;
      version = "latest";
      format = "wheel";
      src = renameWheel wheelFilename wheel;
      inherit
        propagatedBuildInputs
        nativeBuildInputs
        buildInputs
        ;
      doCheck = false;
      dontUsePytestCheck = true;
      meta = {
        inherit description;
        homepage = "https://github.com/agentydragon/ducktape";
        license = lib.licenses.agpl3Only;
      }
      // lib.optionalAttrs (mainProgram != null) { inherit mainProgram; };
    };

  compact-json = pkgs.callPackage ./compact-json.nix { };
  pyrage = pkgs.callPackage ./pyrage.nix { };
  keysymdef = pkgs.callPackage ./keysymdef.nix { };
  asyncvnc = pkgs.callPackage ./asyncvnc.nix { inherit keysymdef; };
in
{
  ducktape-util = mkDucktapeWheel {
    pname = "ducktape-util";
    wheel = ducktape-util-wheel;
    wheelFilename = "ducktape_util-0.1.0-py3-none-any.whl";
    description = "Shared utility library for ducktape wheels";
    propagatedBuildInputs = with pkgs.python3Packages; [ tenacity ];
  };

  ducktape = mkDucktapeWheel {
    pname = "ducktape";
    wheel = ducktape-wheel;
    wheelFilename = "ducktape-0.1.0-py3-none-any.whl";
    description = "CLI tools (git-commit-ai, difftree, gmail-archiver)";
    mainProgram = "git-commit-ai";
    propagatedBuildInputs = with pkgs.python3Packages; [
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
      fastmcp
      mcp
      pyhamcrest
      click
      unidiff
      beautifulsoup4
      google-api-python-client
      google-auth-httplib2
      google-auth-oauthlib
      pydantic-settings
      python-dateutil
      pyyaml
      compact-json
      # skills deps (hetzner-vnc-screenshot)
      hcloud
      pillow
      websockets
      asyncvnc
    ];
  };

  claude-hooks = mkDucktapeWheel {
    pname = "claude-hooks";
    wheel = claude-hooks-wheel;
    wheelFilename = "claude_hooks-0.1.0-py3-none-any.whl";
    description = "Claude Code session hooks (statusline, session-start, auth proxy)";
    mainProgram = "claude-hook";
    propagatedBuildInputs =
      with pkgs.python3Packages;
      [
        anyio
        cryptography
        fastapi
        httpx
        kubernetes
        mako
        opentelemetry-api
        opentelemetry-exporter-otlp-proto-http
        opentelemetry-sdk
        platformdirs
        psutil
        pydantic
        pydantic-settings
        pygit2
        pyjwt
        pyyaml
        rich
        structlog
        supervisor
        tenacity
        uvicorn
      ]
      ++ [
        pkgs.pre-commit
        pyrage
      ];
  };

  gterm-theme = mkDucktapeWheel {
    pname = "gterm-theme";
    wheel = gterm-theme-wheel;
    wheelFilename = "gterm_theme-0.1.0-py3-none-any.whl";
    description = "GNOME Terminal theme follower";
    mainProgram = "gterm-theme";
    nativeBuildInputs = with pkgs; [
      gobject-introspection
      wrapGAppsHook3
    ];
    buildInputs = with pkgs; [
      glib
      dbus
      cairo
      gtk3
    ];
    propagatedBuildInputs = with pkgs.python3Packages; [
      absl-py
      dbus-python
      pycairo
      pygobject3
    ];
  };
}
