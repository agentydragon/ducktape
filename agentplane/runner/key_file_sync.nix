{ pkgs }:
pkgs.writeShellApplication {
  name = "agentplane-key-file-sync";
  runtimeInputs = [
    pkgs.kubectl
    pkgs.python3
  ];
  text = ''
    exec ${pkgs.python3}/bin/python3 ${./key_file_sync.py} "$@"
  '';
}
