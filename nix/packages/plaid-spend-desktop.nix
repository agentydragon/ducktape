{
  artifacts,
  lib,
  pkgs,
  python314Packages,
}:
let
  extensionUuid = "plaid-spend@allegedly.works";
  extensionZip = artifacts."plaid-spend-desktop-extension";
in
python314Packages.buildPythonApplication {
  pname = "plaid-spend-desktop";
  version = "0.1.0";
  format = "wheel";
  src = artifacts."plaid-spend-desktop";

  propagatedBuildInputs = with python314Packages; [
    babel
    dbus-next
    httpx
    pydantic
  ];
  pythonImportsCheck = [
    "finance.plaid.spend.desktop.cli"
    "finance.plaid.spend.desktop.daemon"
  ];
  doCheck = false;
  dontUsePytestCheck = true;

  nativeBuildInputs = [
    pkgs.makeWrapper
    pkgs.unzip
  ];
  postInstall = ''
    extensionDir="$out/share/gnome-shell/extensions/${extensionUuid}"
    mkdir -p "$extensionDir"
    unzip -o ${extensionZip} -d "$extensionDir"

    wrapProgram "$out/bin/plaid-spend-daemon" \
      --prefix PATH : ${
        lib.makeBinPath [
          pkgs.libsecret
          pkgs.xdg-utils
        ]
      }
  '';

  passthru.extensionUuid = extensionUuid;

  meta = {
    description = "CLI and GNOME clients for server-computed Plaid spend and allowance";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "plaid-spend";
    platforms = lib.platforms.linux;
  };
}
