{
  artifacts,
  lib,
  pkgs,
  python314Packages,
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
  propagatedBuildInputs = with python314Packages; [
    atomicwrites
    httpx
    platformdirs
    pydantic
    typer
  ];
  pythonImportsCheck = [ "aiquota.cli" ];
  doCheck = false;
  dontUsePytestCheck = true;

  nativeBuildInputs = [ pkgs.unzip ];
  postInstall = ''
    uuid="aiquota@allegedly.works"
    extDir="$out/share/gnome-shell/extensions/$uuid"
    mkdir -p "$extDir"
    unzip -o ${extensionZip} -d "$extDir"
    ln -s ../../../../bin/aiquota "$extDir/aiquota"
  '';

  passthru.extensionUuid = "aiquota@allegedly.works";

  meta = {
    description = "AI subscription quota tracker (CLI + GNOME Shell extension)";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "aiquota";
  };
}
