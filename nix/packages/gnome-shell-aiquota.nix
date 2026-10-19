{
  artifacts,
  lib,
  pkgs,
  python314Packages,
  claudeApi,
}:
let
  wheel = artifacts.aiquota;
  extensionZip = artifacts.aiquota-extension;
in
python314Packages.buildPythonApplication {
  pname = "aiquota";
  version = "latest";
  format = "wheel";
  src = wheel;
  # SYNC: Match `ducktape-claude-api` in //aiquota:aiquota_wheel's `requires`.
  propagatedBuildInputs =
    with python314Packages;
    [
      atomicwrites
      dbus-next
      httpx
      httpx-sse
      platformdirs
      pydantic
      typer
    ]
    ++ [ claudeApi ];
  pythonImportsCheck = [ "aiquota.cli" ];
  doCheck = false;
  dontUsePytestCheck = true;

  preDistPhases = [ "aiquotaConsoleScriptsImportCheck" ];
  aiquotaConsoleScriptsImportCheck = ''
        export PYTHONPATH="$out/${python314Packages.python.sitePackages}:$PYTHONPATH"
        (cd "$out" && ${python314Packages.python.interpreter} -c '
    import importlib.metadata

    entry_points = [
        entry_point
        for entry_point in importlib.metadata.distribution("aiquota").entry_points
        if entry_point.group == "console_scripts"
    ]
    if not entry_points:
        raise RuntimeError("aiquota wheel has no console-script entry points")
    for entry_point in entry_points:
        entry_point.load()
    ')
  '';

  nativeBuildInputs = [ pkgs.unzip ];
  postInstall = ''
    uuid="aiquota@allegedly.works"
    extDir="$out/share/gnome-shell/extensions/$uuid"
    mkdir -p "$extDir"
    unzip -o ${extensionZip} -d "$extDir"
  '';

  passthru.extensionUuid = "aiquota@allegedly.works";

  meta = {
    description = "AI subscription quota tracker (CLI + GNOME Shell extension)";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "aiquota";
  };
}
