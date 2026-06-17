# Ducktape packages — built from CI-released artifacts (nix/artifact-pins.json).
{
  lib,
  pkgs,
  pkgsUnstable,
  artifacts,
}:
let
  # The ducktape umbrella wheel is built (on Bazel) against
  # `fastmcp>=3.0` + `mcp==1.26.0` (see requirements_bazel.txt). nixpkgs
  # 25.11 ships fastmcp 2.12 + mcp 1.15, and even nixos-unstable only has
  # fastmcp 2.14. To let the MCP-using entry points (`git-commit-ai`,
  # `gmail-archiver`) import on NixOS we vendor mcp 1.26 (rebuilt against
  # the stable python interpreter) and the three packages that fastmcp 3
  # needs but nixpkgs doesn't carry: fastmcp itself, uncalled-for, and
  # py-key-value-aio. Applied via a python interpreter override so the
  # whole closure shares one consistent site-packages.
  python3 = pkgs.python3.override {
    self = python3;
    packageOverrides = pyfinal: pyprev: {
      mcp = pyprev.mcp.overridePythonAttrs (old: {
        version = "1.26.0";
        inherit (pkgsUnstable.python3Packages.mcp) src;
        # 1.26.0 added pyjwt + typing-inspection to its runtime deps
        # (nixpkgs 25.11's derivation still encodes the 1.15.0 set).
        dependencies =
          old.dependencies
          ++ (with pyprev; [
            pyjwt
            cryptography
            typing-inspection
          ]);
      });
      uncalled-for = pkgs.callPackage ./uncalled-for.nix {
        python3Packages = pyfinal;
      };
      py-key-value-aio = pkgs.callPackage ./py-key-value-aio.nix {
        python3Packages = pyfinal;
      };
      fastmcp = pkgs.callPackage ./fastmcp.nix {
        python3Packages = pyfinal;
        inherit (pyfinal) py-key-value-aio uncalled-for;
      };
    };
  };
  python3Packages = python3.pkgs;
  # CI wheels land in the nix store as "source" (no .whl extension).
  # pypaInstallPhase globs *.whl, so we restore the original filename.
  renameWheel =
    name: input:
    pkgs.runCommand name { } ''
      cp ${input} $out
    '';

  # All ducktape wheels follow the same pattern: pname maps to an artifact-pin
  # artifact, wheel filename is <pname_underscored>-0.1.0-py3-none-any.whl.
  #
  # `importsCheck` is required — at minimum list the modules backing each
  # console-script entry point. buildPythonApplication imports them at build
  # time, so a missing propagatedBuildInputs surfaces as a build failure
  # instead of a runtime crash. See debug/aiquota-missing-atomicwrites.md.
  mkWheel =
    {
      pname,
      description,
      importsCheck,
      propagatedBuildInputs ? [ ],
      nativeBuildInputs ? [ ],
      buildInputs ? [ ],
      mainProgram ? null,
    }:
    python3Packages.buildPythonApplication {
      inherit pname;
      version = "latest";
      format = "wheel";
      src = renameWheel "${
        builtins.replaceStrings [ "-" ] [ "_" ] pname
      }-0.1.0-py3-none-any.whl" artifacts.${pname};
      inherit propagatedBuildInputs buildInputs;
      nativeBuildInputs = nativeBuildInputs ++ [ pkgs.cacert ];
      # pygit2 (and anything else that calls OpenSSL at module import) needs
      # a CA bundle visible during the imports-check phase, otherwise libgit2
      # fails with "failed to load certificates" inside the sealed build env.
      # stdenv pins SSL_CERT_FILE to /no-cert-file.crt when unset, AND nixpkgs'
      # python3Packages.httpx ships a postHook that runs `unset SSL_CERT_FILE`
      # after every build phase — so neither `env.SSL_CERT_FILE` nor cacert's
      # own setup-hook survive long enough. Override the stock phase with one
      # that sets the env var inside the same shell invocation.
      dontUsePythonImportsCheck = true;
      preDistPhases = [ "ducktapePythonImportsCheck" ];
      ducktapePythonImportsCheck = ''
        export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
        echo "Check imports (SSL_CERT_FILE pinned): ${builtins.concatStringsSep " " importsCheck}"
        export PYTHONPATH="$out/lib/${python3.libPrefix}/site-packages:$PYTHONPATH"
        (cd "$out" && ${python3.interpreter} -c \
          'import sys, importlib; [importlib.import_module(m) for m in sys.argv[1:]]' \
          ${builtins.concatStringsSep " " importsCheck})
      '';
      doCheck = false;
      dontUsePytestCheck = true;
      meta = {
        inherit description;
        homepage = "https://github.com/agentydragon/ducktape";
        license = lib.licenses.agpl3Only;
      }
      // lib.optionalAttrs (mainProgram != null) { inherit mainProgram; };
    };

  # Python packages not in nixpkgs (used as propagatedBuildInputs)
  compact-json = pkgs.callPackage ./compact-json.nix { };
  pyrage = pkgs.callPackage ./pyrage.nix { };
  keysymdef = pkgs.callPackage ./keysymdef.nix { };
  asyncvnc = pkgs.callPackage ./asyncvnc.nix { inherit keysymdef; };
  ducktape-util = mkWheel {
    pname = "ducktape-util";
    description = "Shared utility library (util.bazel, util.fs, etc.)";
    importsCheck = [ "util" ];
    propagatedBuildInputs = with python3Packages; [
      opentelemetry-api
      opentelemetry-sdk
      tenacity
    ];
  };

  ducktape-git-hooks = mkWheel {
    pname = "ducktape-git-hooks";
    description = "Git hooks: ducktape-precommit, ducktape-commit-msg, cluster validation";
    mainProgram = "ducktape-precommit";
    importsCheck = [ "devinfra.precommit.git_hook" ];
    # SYNC: This list must match `requires` in //:ducktape_git_hooks_wheel (BUILD.bazel).
    # When adding a dependency, update BOTH places.
    propagatedBuildInputs =
      with python3Packages;
      [
        httpx
        networkx
        opentelemetry-api
        opentelemetry-sdk
        protobuf
        pygit2
        pydantic
        pyyaml
      ]
      ++ [ ducktape-util ];
  };

  # Combined CLI + GNOME Shell extension package.
  aiquota = pkgs.callPackage ./gnome-shell-aiquota.nix { inherit artifacts lib; };

  mkBinaryArtifact =
    {
      pname,
      src,
      binaryName ? pname,
      description,
    }:
    pkgs.stdenvNoCC.mkDerivation {
      inherit pname src;
      version = "latest";
      nativeBuildInputs = [ pkgs.autoPatchelfHook ];
      buildInputs = [ pkgs.stdenv.cc.cc.lib ];
      dontUnpack = true;
      installPhase = ''
        install -Dm755 $src $out/bin/${binaryName}
      '';
      meta = {
        inherit description;
        homepage = "https://github.com/agentydragon/ducktape";
        license = lib.licenses.agpl3Only;
        mainProgram = binaryName;
        platforms = [ "x86_64-linux" ];
      };
    };

  debundle = mkBinaryArtifact {
    pname = "debundle";
    src = artifacts.debundle;
    description = "JavaScript debundling CLI";
  };

in
rec {
  inherit ducktape-util;
  inherit ducktape-git-hooks;
  inherit aiquota;

  bbr = mkWheel {
    pname = "bbr";
    description = "bb remote wrapper with repo-level config from devinfra/bbr.json";
    mainProgram = "bbr";
    importsCheck = [ "devinfra.bbr" ];
    propagatedBuildInputs = with python3Packages; [ pygit2 ];
  };

  ducktape = mkWheel {
    pname = "ducktape";
    description = "CLI tools (git-commit-ai, difftree, gmail-archiver)";
    mainProgram = "git-commit-ai";
    importsCheck = [
      "difftree.cli"
      "git_commit_ai.cli"
      "gmail_archiver.main"
      "cluster.skills.hetzner_vnc_screenshot.vnc_screenshot"
      "cluster.skills.proxmox_vm.vm_interact"
    ];
    propagatedBuildInputs = with python3Packages; [
      aiodocker
      anyio
      httpx
      jinja2
      mako
      openai
      platformdirs
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
      # skills deps (hetzner-vnc-screenshot, proxmox_vm)
      hcloud
      pillow
      websockets
      asyncvnc
      ducktape-util
    ];
  };

  claude-hooks = mkWheel {
    pname = "claude-hooks";
    description = "Python Claude Code statusline";
    mainProgram = "claude-statusline";
    importsCheck = [ "devinfra.claude.statusline.statusline" ];
    # SYNC: This list must match `requires` in //:claude_hooks_wheel (BUILD.bazel).
    # The wheel declares pip-level deps; this list provides Nix-level equivalents.
    # When adding a dependency, update BOTH places.
    #
    propagatedBuildInputs = with python3Packages; [
      httpx
      platformdirs
      pydantic
      rich
    ];
  };

  # Expose only the Python statusline command. Active Claude hook dispatch is the
  # Rust binary below, so this avoids putting the legacy Python `claude-hook` on PATH.
  claude-statusline = pkgs.runCommand "claude-statusline" { } ''
    mkdir -p $out/bin
    ln -s ${claude-hooks}/bin/claude-statusline $out/bin/claude-statusline
  '';

  # Rust claude-hook binary — static, no runtime deps.
  # Provides the active `claude-hook` binary used for hook dispatch and shims.
  claude-hook-rs = pkgs.stdenvNoCC.mkDerivation {
    pname = "claude-hook-rs";
    version = "latest";
    src = artifacts.claude-hook-rs;
    dontUnpack = true;
    installPhase = ''
      install -Dm755 $src $out/bin/claude-hook
    '';
    meta = {
      description = "Claude Code hook daemon (Rust)";
      homepage = "https://github.com/agentydragon/ducktape";
      license = lib.licenses.agpl3Only;
      mainProgram = "claude-hook";
    };
  };

  gterm-theme = mkWheel {
    pname = "gterm-theme";
    description = "GNOME Terminal theme follower";
    mainProgram = "gterm-theme";
    importsCheck = [ "gnome.gterm_theme.main" ];
    nativeBuildInputs = with pkgs; [
      gobject-introspection
      wrapGAppsHook3
    ];
    buildInputs = with pkgs; [
      glib
      dbus
      cairo
      gtk3
      # Provides org.gnome.Terminal.ProfilesList, which gterm-theme reads via
      # Gio.Settings at startup.
      gnome-terminal
    ];
    propagatedBuildInputs = with python3Packages; [
      absl-py
      dbus-python
      pycairo
      pygobject3
    ];
  };

  # Alias for programs.gnome-shell.extensions compatibility.
  gnome-shell-aiquota = aiquota;
  tana-outliner = pkgs.callPackage ./tana-outliner.nix { };
  gmail-mcp = pkgs.callPackage ./gmail-mcp.nix { };
  foxflss = pkgs.callPackage ./foxflss.nix { };
  litert-lm = pkgs.callPackage ./litert-lm.nix { };
  prettier = pkgs.callPackage ./prettier/prettier.nix { };
  kubernetes-mcp-server = pkgs.callPackage ./kubernetes-mcp-server.nix { };
  bebas-neue-font = pkgs.callPackage ./bebas-neue-font.nix { };
  bb = pkgs.callPackage ./bb.nix { inherit artifacts; };
  telegram-desktop = pkgs.callPackage ./telegram-desktop.nix { };

  bbapi = pkgs.stdenv.mkDerivation {
    pname = "bbapi";
    version = "latest";
    src = artifacts.bbapi;
    dontUnpack = true;
    installPhase = ''
      mkdir -p $out/bin
      cp $src $out/bin/bbapi
      chmod +x $out/bin/bbapi
    '';
    meta = {
      description = "BuildBuddy API CLI";
      homepage = "https://github.com/agentydragon/ducktape";
      license = lib.licenses.agpl3Only;
      mainProgram = "bbapi";
      platforms = [ "x86_64-linux" ];
    };
  };

  # Skills data: $out/share/claude-hooks/skills/
  skills = pkgs.runCommand "claude-hooks-skills" { nativeBuildInputs = [ pkgs.unzip ]; } ''
    mkdir -p $out/share/claude-hooks/skills
    unzip -q ${artifacts.skills} -d $out/share/claude-hooks/skills
  '';
}
// lib.optionalAttrs (artifacts ? debundle) {
  inherit debundle;
}
