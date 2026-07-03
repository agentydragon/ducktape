# Shared Home Manager SSH defaults.
{ lib, config, ... }:
let
  cfg = config.ducktape.ssh;

  defaultMatchBlock = {
    forwardAgent = false;
    addKeysToAgent = "no";
    compression = false;
    serverAliveInterval = 0;
    serverAliveCountMax = 3;
    hashKnownHosts = false;
    userKnownHostsFile = "~/.ssh/known_hosts";
    controlMaster = "no";
    controlPath = "~/.ssh/master-%r@%n:%p";
    controlPersist = "no";
  };
in
{
  options.ducktape.ssh.enable = lib.mkEnableOption "shared SSH client configuration";

  config = lib.mkIf cfg.enable {
    programs.ssh = {
      enable = true;
      enableDefaultConfig = false;
      matchBlocks."*" = lib.mkDefault defaultMatchBlock;
    };
  };
}
