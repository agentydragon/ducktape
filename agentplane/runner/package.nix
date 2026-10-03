# The one runner package consumed by both the OCI sandbox image and the NixOS guest.
{
  pkgsUnstable,
  wheel,
}:
let
  python = pkgsUnstable.python314;
in
python.pkgs.buildPythonApplication {
  pname = "agentplane-runner";
  version = "latest";
  format = "wheel";
  src = wheel;
  dependencies = with python.pkgs; [
    aiosqlite
    greenlet # sqlalchemy's asyncio extension
    grpcio
    protobuf
    pydantic
    sqlalchemy
    typer
  ];
  pythonImportsCheck = [
    "agentplane.runner.main"
  ];
}
