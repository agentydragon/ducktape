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
