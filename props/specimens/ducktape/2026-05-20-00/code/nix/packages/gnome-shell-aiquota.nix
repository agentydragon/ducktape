{
  artifacts,
  lib,
  pkgs,
}:
let
  wheel = artifacts.aiquota;
  extensionZip = artifacts.aiquota-extension;
in
pkgs.python3Packages.buildPythonApplication {
  pname = "aiquota";
  version = "latest";
  format = "wheel";
  src = wheel;
  propagatedBuildInputs = with pkgs.python3Packages; [
    httpx
    platformdirs
    pydantic
    typer
  ];
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
