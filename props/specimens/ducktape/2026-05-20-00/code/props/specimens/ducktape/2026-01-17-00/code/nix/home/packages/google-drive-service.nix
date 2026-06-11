{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.google-drive;

  google-drive = pkgs.stdenv.mkDerivation rec {
    pname = "google-drive-file-stream";
    version = "112.0.3";

    # Impure fetch from private git repo (uses credential helper)
    src = "${builtins.fetchGit {
      url = "https://git.k3s.agentydragon.com/agentydragon/google-drive";
      ref = "main";
    }}/${version}.zip";

    nativeBuildInputs = [
      pkgs.unzip
      pkgs.autoPatchelfHook
    ];
    buildInputs = [pkgs.fuse];

    dontBuild = true;

    unpackPhase = ''
      unzip $src
    '';

    installPhase = ''
      mkdir -p $out/bin
      mkdir -p $out/lib
      mkdir -p $out/share/google-drive

      # Install binaries
      install -m 755 drive $out/bin/google-drive
      install -m 755 directoryprefetcher_binary $out/bin/directoryprefetcher_binary

      # Install library
      install -m 644 libfuse.so $out/lib/

      # Install support files
      install -m 644 roots.pem $out/share/google-drive/
      install -m 755 drive-filter.py $out/share/google-drive/
    '';

    meta = with pkgs.lib; {
      description = "Google Drive File Stream client";
      platforms = platforms.linux;
    };
  };
in {
  options.services.google-drive = {
    enable = lib.mkEnableOption "Google Drive File Stream service";
  };

  config = lib.mkIf cfg.enable {
    home.packages = [google-drive];

    systemd.user.services.google-drive = {
      Unit = {
        Description = "Google Drive service";
        After = ["network.target"];
      };
      Service = {
        Type = "simple";
        Restart = "no";
        ExecStart = "${google-drive}/bin/google-drive %h/.google-drive";
        StandardOutput = "journal";
        StandardError = "journal";
      };
      Install = {
        WantedBy = ["default.target"];
      };
    };

    # Create symlinks for Google Drive
    # Note: The actual .google-drive mount directory will be created by the drive binary
    home.file = {
      # Symlink ~/drive -> ~/.google-drive/My Drive
      "drive" = {
        source = config.lib.file.mkOutOfStoreSymlink "${config.home.homeDirectory}/.google-drive/My Drive";
      };

      # Symlink Worthy config from Drive
      ".config/worthy/config.yaml" = {
        source = config.lib.file.mkOutOfStoreSymlink "${config.home.homeDirectory}/drive/finance/worthy-config.yaml";
      };
    };
  }; # Close config = lib.mkIf
}
