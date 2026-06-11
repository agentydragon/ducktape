# Custom datetime indicator format configuration
{
  config,
  lib,
  enableGui,
  ...
}: {
  config = lib.mkIf enableGui {
    # Ubuntu Unity datetime indicator customization
    # Sets ISO 8601 format with seconds: YYYY-MM-DD HH:MM:SS
    dconf.settings."com/canonical/indicator/datetime" = {
      time-format = "custom";
      custom-time-format = "%Y-%m-%d %H:%M:%S";
      show-week-numbers = true;
    };
  };
}
