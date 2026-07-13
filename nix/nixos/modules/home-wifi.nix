# Home Wi-Fi connection rendered from a SOPS secret at activation time.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.homeWifi;
in
{
  options.ducktape.homeWifi.enable = lib.mkEnableOption "the SOPS-managed home Wi-Fi connection";

  config = lib.mkIf cfg.enable {
    # Keep both connection values out of the Nix store. The host's SOPS age
    # identity decrypts them while activating the NetworkManager keyfile.
    sops.secrets = {
      home_wifi_ssid = {
        sopsFile = ../../../secrets/shared/home-wifi.yaml;
        key = "ssid";
      };
      home_wifi_password = {
        sopsFile = ../../../secrets/shared/home-wifi.yaml;
        key = "password";
      };
    };

    sops.templates.home_wifi_nm_connection = {
      content = ''
        [connection]
        id=home-wifi
        type=wifi
        autoconnect=true

        [wifi]
        ssid=${config.sops.placeholder.home_wifi_ssid}
        mode=infrastructure

        [wifi-security]
        key-mgmt=wpa-psk
        psk=${config.sops.placeholder.home_wifi_password}

        [ipv4]
        method=auto

        [ipv6]
        method=auto
      '';
      path = "/etc/NetworkManager/system-connections/home-wifi.nmconnection";
      mode = "0600";
    };
  };
}
