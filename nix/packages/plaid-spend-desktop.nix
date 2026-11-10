{
  artifacts,
  lib,
  pkgs,
  python314Packages,
  networkMonitor,
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
    aiohttp
    authlib
    babel
    dbus-next
    httpx
    httpx2
    pydantic
    pyyaml
    networkMonitor
  ];
  pythonImportsCheck = [
    "finance.plaid.spend.desktop.cli"
    "finance.plaid.spend.desktop.daemon"
    "util.network_monitor"
  ];
  doCheck = false;
  dontUsePytestCheck = true;

  nativeBuildInputs = [
    pkgs.makeWrapper
    pkgs.glib
    pkgs.unzip
  ];
  postInstall = ''
    extensionDir="$out/share/gnome-shell/extensions/${extensionUuid}"
    mkdir -p "$extensionDir"
    unzip -o ${extensionZip} -d "$extensionDir"
    if [ -d "$extensionDir/schemas" ]; then
      glib-compile-schemas "$extensionDir/schemas"
    fi

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
