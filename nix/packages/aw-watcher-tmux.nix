{
  lib,
  stdenvNoCC,
  fetchFromGitHub,
  makeWrapper,
  bash,
  coreutils,
  curl,
  gawk,
  tmux,
}:

stdenvNoCC.mkDerivation {
  pname = "aw-watcher-tmux";
  version = "unstable";

  src = fetchFromGitHub {
    owner = "akohlbecker";
    repo = "aw-watcher-tmux";
    rev = "efaa7610add52bd2b39cd98d0e8e082b1e126487";
    hash = "sha256-L6YLyEOmb+vdz6bJdB0m5gONPpBp2fV3i9PiLSNrZNM=";
  };

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    install -Dm755 scripts/monitor-session-activity.sh $out/bin/aw-watcher-tmux
    patchShebangs $out/bin/aw-watcher-tmux
    # The upstream plugin logs every heartbeat payload and its temporary filename.
    # Keep the systemd journal useful while preserving actual error output.
    sed -i '/^echo \$TMP_FILE$/d; /^    echo "\$PAYLOAD"$/d' $out/bin/aw-watcher-tmux
    wrapProgram $out/bin/aw-watcher-tmux \
      --prefix PATH : ${
        lib.makeBinPath [
          bash
          coreutils
          curl
          gawk
          tmux
        ]
      }

    runHook postInstall
  '';

  meta = {
    description = "ActivityWatch watcher for tmux session and pane activity";
    homepage = "https://github.com/akohlbecker/aw-watcher-tmux";
    license = lib.licenses.mit;
    mainProgram = "aw-watcher-tmux";
    platforms = lib.platforms.unix;
  };
}
