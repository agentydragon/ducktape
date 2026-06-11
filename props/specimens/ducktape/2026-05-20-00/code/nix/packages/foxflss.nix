# Foxconn FoxFlss — FCC unlock and RF calibration tool for DW5932e/DW5934e modems.
#
# Closed-source binary from the foxconn-pc/fii_linux GitHub repo. Provides:
#   - FoxFlss (bare): FCC unlock, allows the software radio to turn on.
#   - FoxFlss -f Check_RF_SSKU: RF calibration, writes RF tuner settings, DPR
#     tables, and NR carrier aggregation configs to modem non-volatile storage.
#   - DW5932e_RF.dat, DW5934e_RF.dat: platform-specific RF calibration data files.
#
# FoxFlss shells out to several tools (per debug/rugged/hw/foxflss_wwan.md
# "FoxFlss Tool Dependencies"): dmidecode (system SKU lookup, REQUIRED for
# FCC unlock — without it FoxFlss prints "Current platform: do not support
# FccLock!" and exits 1), lspci, pgrep, tar/gzip (RF cal data extraction),
# and basic shell utils (grep/sed/awk/coreutils). The binary is wrapped with
# all of these on PATH so callers don't need to compose the PATH themselves.
#
# FoxFlss hardcodes /opt/foxconn/data/ for the .dat files; see foxconn-wwan.nix
# for the systemd-tmpfiles symlinks that put them there.
{ pkgs, lib }:

let
  foxflss-unwrapped = pkgs.stdenv.mkDerivation {
    pname = "foxflss";
    version = "1.0.15";

    src = pkgs.fetchFromGitHub {
      owner = "foxconn-pc";
      repo = "fii_linux";
      rev = "c4a3f92f1a1d11dd08b92f5adb5bc1800a115f28";
      hash = "sha256-z/hIWJOyHSM3xN99cKSIXJwfu6+/q3NbV6SSNO4md7g=";
    };

    nativeBuildInputs = [ pkgs.autoPatchelfHook ];
    buildInputs = [ pkgs.glibc ];

    dontBuild = true;

    installPhase = ''
      runHook preInstall
      install -Dm755 Application/FoxFlss/bin/FoxFlss $out/libexec/foxflss/FoxFlss
      install -Dm644 Application/FoxFlss/data/DW5932e_RF.dat $out/share/foxflss/DW5932e_RF.dat
      install -Dm644 Application/FoxFlss/data/DW5934e_RF.dat $out/share/foxflss/DW5934e_RF.dat
      runHook postInstall
    '';
  };

  # See package header for the dependency list and rationale.
  runtimePATH = lib.makeBinPath [
    pkgs.dmidecode # dmidecode — system SKU lookup (REQUIRED for FCC unlock)
    pkgs.pciutils # lspci
    pkgs.gnugrep # grep
    pkgs.gnused # sed
    pkgs.gawk # awk
    pkgs.procps # pgrep
    pkgs.gnutar # tar
    pkgs.gzip # gzip (used by tar -z)
    pkgs.coreutils # cat, echo, etc.
  ];

  # Wrapped FoxFlss with the correct PATH pre-set. The wrapper script
  # sets PATH then execs the real binary so callers get a ready-to-use binary.
  foxflss-wrapped = pkgs.runCommand "${foxflss-unwrapped.pname}-wrapped" { } ''
    mkdir -p $out/bin
    cat > $out/bin/FoxFlss << WRAPPER
    #!${pkgs.bash}/bin/bash
    export PATH="${runtimePATH}:$PATH"
    exec ${foxflss-unwrapped}/libexec/foxflss/FoxFlss "$@"
    WRAPPER
    chmod +x $out/bin/FoxFlss
    ln -s ${foxflss-unwrapped}/share $out/share
  '';

in
pkgs.symlinkJoin {
  name = foxflss-unwrapped.pname;
  paths = [
    foxflss-wrapped
    foxflss-unwrapped
  ];
}
