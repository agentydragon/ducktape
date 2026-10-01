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
  ];
  pythonImportsCheck = [
    "plaid_spend_desktop.cli"
    "plaid_spend_desktop.daemon"
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
    description = "CLI and GNOME clients for server-computed Plaid statement-cycle spend";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "plaid-spend";
    platforms = lib.platforms.linux;
  };
}
