# SSH key for accessing the Home Assistant box at 15 Leroy (10.0.0.3).
# Decrypted from SOPS at home-manager activation time using ~/.ssh/id_ed25519.
{ config, ... }:
{
  sops.secrets.ha_15leroy_ssh_key = {
    sopsFile = ../../../secrets/15leroy-homeassistant-ssh.yaml;
    key = "ssh_private_key";
    path = "${config.home.homeDirectory}/.ssh/15leroy";
    mode = "0600";
  };
}
