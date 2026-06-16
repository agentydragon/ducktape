# Claude Code web session — standalone home-manager profile.
#
# Headless, non-NixOS. This is the home-manager counterpart to the
# `nix profile install .#devtools` + manual skill-symlink path in
# <devinfra/claude/web_setup.sh>: it installs the same devtools and deploys
# Claude Code settings + skills through the shared home-manager skills module
# (../skills.nix, via the ../claude_code module).
#
# Portable across whatever user the web container runs as: home.username and
# home.homeDirectory are read from the environment, so activation needs
# --impure (same as the atlas profile):
#
#   home-manager switch --impure --flake .#claude-web
{
  pkgs,
  lib,
  webDevTools,
  ...
}:
let
  envUser = builtins.getEnv "USER";
  envHome = builtins.getEnv "HOME";
in
{
  imports = [
    ../claude_code
  ];

  home.username = if envUser != "" then envUser else "user";
  home.homeDirectory =
    if envHome != "" then envHome else "/home/${if envUser != "" then envUser else "user"}";
  home.stateVersion = "25.11";

  programs.home-manager.enable = true;
  targets.genericLinux.enable = true;

  nix.package = pkgs.nix;
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  # direnv + nix-direnv with the bash hook wired in (programs.bash.enable is
  # required for home-manager to emit the hook into the shell rc) so `cd`ing into
  # the repo activates the .envrc devshell.
  programs.bash.enable = true;
  programs.direnv = {
    enable = true;
    enableBashIntegration = true;
    nix-direnv.enable = true;
  };

  # Same toolset the .#devtools profile install ships (claude-hook, statusline,
  # bbr, bbapi, gh, sops, kubectl, …). Reuses the flake's devToolPackages list
  # so the two install paths can never drift.
  home.packages = webDevTools;
}
