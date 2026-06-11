{
  config,
  pkgs,
  lib,
  enableGui ? true,
  ...
}:
lib.mkIf enableGui {
  # COSMIC desktop shortcuts configuration
  xdg.configFile."cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom".source =
    ../../../../../../.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom;
}
