{
  pkgs,
  vmConfig,
}:
let
  prepareCommand = vmConfig.systemd.services.agentplane-workspace-prepare.serviceConfig.ExecStart;
  workspaceDevice = vmConfig.fileSystems."/state/workspaces".device;
in
pkgs.runCommand "agentplane-vm-workspace-startup-check" { } ''
  set -- ${prepareCommand}
  test "$#" -eq 2
  test -x "$1"
  test "$2" = ${pkgs.lib.escapeShellArg workspaceDevice}
  touch "$out"
''
