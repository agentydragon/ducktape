# One-shot service to bootstrap home-manager on first boot
# Creates flag file after success so it only runs once
{
  config,
  pkgs,
  lib,
  username,
  homeManagerHost,
  ...
}: {
  systemd.services.home-manager-init = {
    description = "Initial home-manager setup";
    after = ["network-online.target" "nix-daemon.service"];
    wants = ["network-online.target"];
    requires = ["nix-daemon.service"];
    wantedBy = ["multi-user.target"];
    path = [pkgs.nix pkgs.git];
    unitConfig = {
      ConditionPathExists = "!/home/${username}/.home-manager-init-done";
    };
    serviceConfig = {
      Type = "oneshot";
      User = username;
      # Create profile directory if it doesn't exist
      ExecStartPre = "${pkgs.coreutils}/bin/mkdir -p /home/${username}/.local/state/nix/profiles";
      ExecStart = "${pkgs.writeShellScript "home-manager-init" ''
        set -e
        export HOME=/home/${username}
        export USER=${username}
        export NIX_PATH=nixpkgs=${pkgs.path}
        # Ensure nix profile directories exist
        mkdir -p ~/.local/state/nix/profiles
        mkdir -p ~/.nix-profile
        ${pkgs.home-manager}/bin/home-manager switch \
          --flake "github:agentydragon/ducktape?dir=nix/home&ref=devel#${homeManagerHost}" \
          --impure \
          2>&1 | tee ~/home-manager-init.log
      ''}";
      ExecStartPost = "${pkgs.coreutils}/bin/touch /home/${username}/.home-manager-init-done";
      RemainAfterExit = true;
    };
  };
}
