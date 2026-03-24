# claude-hooks: Claude Code session hooks (statusline, session-start, auth proxy)
# Wheel fetched as a flake input (claude-hooks-wheel) from GitHub Releases.
{
  lib,
  pkgs,
  claude-hooks-wheel,
}:
pkgs.python3Packages.buildPythonApplication {
  pname = "claude-hooks";
  version = "latest";
  format = "wheel";

  src = claude-hooks-wheel;

  propagatedBuildInputs =
    (with pkgs.python3Packages; [
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
    ])
    # TODO: pkgs.pre-commit is a system package dep rather than a Python dep;
    # consider whether it should be a native build input, a wrapper script PATH
    # injection, or left to the environment (home.nix) instead of propagated here.
    ++ [ pkgs.pre-commit ];

  # Disable checks - wheel is tested in CI
  doCheck = false;

  meta = {
    description = "Claude Code session hooks (statusline, session-start, auth proxy)";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "claude-hook";
  };
}
