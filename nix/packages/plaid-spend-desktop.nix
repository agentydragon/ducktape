{
  lib,
  pkgs,
  python314,
}:
let
  src = ../../finance/plaid/spend/desktop;
  extensionUuid = "plaid-spend@allegedly.works";
  pythonEnv = python314.withPackages (pythonPackages: [
    pythonPackages.dbus-next
    pythonPackages.httpx
  ]);
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "plaid-spend-desktop";
  version = "0.1.0";
  dontUnpack = true;

  nativeBuildInputs = [ pkgs.makeWrapper ];

  installPhase = ''
    runHook preInstall

    extDir="$out/share/gnome-shell/extensions/${extensionUuid}"
    install -d "$extDir" "$out/lib/plaid-spend-desktop" "$out/bin"
    cp -r ${src}/src/plaid_spend_desktop "$out/lib/plaid-spend-desktop/"
    install -Dm644 ${src}/gnome/metadata.json "$extDir/metadata.json"
    install -Dm644 ${src}/gnome/extension.js "$extDir/extension.js"
    makeWrapper ${pythonEnv}/bin/python "$out/bin/plaid-spend-daemon" \
      --add-flags "-m plaid_spend_desktop.daemon" \
      --prefix PYTHONPATH : "$out/lib/plaid-spend-desktop" \
      --prefix PATH : ${
        lib.makeBinPath [
          pkgs.libsecret
          pkgs.xdg-utils
        ]
      }

    runHook postInstall
  '';

  passthru.extensionUuid = extensionUuid;

  meta = {
    description = "GNOME panel client for server-computed Plaid statement-cycle spend";
    homepage = "https://github.com/agentydragon/ducktape";
    license = lib.licenses.agpl3Only;
    mainProgram = "plaid-spend-daemon";
    platforms = lib.platforms.linux;
  };
}
