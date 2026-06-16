# Claude Code web session — standalone, minimal home-manager profile.
#
# Deliberately independent of the shared nix/home host structure (it does NOT
# import home.nix or the claude_code module). It contains only what
# web_setup.sh's home-manager mode explicitly needs:
#   - the devtools (same list the .#devtools profile install ships)
#   - direnv + nix-direnv
#   - skill deployment into ~/.claude/skills via the shared skills module
#
# The Claude Code CLI itself is provided by Anthropic in the web session, so it
# is not installed here (and the nixpkgs build downloads the binary from
# downloads.claude.ai, which 403s in the web container anyway).
#
# Portable across whatever user the web container runs as: home.username and
# home.homeDirectory are read from the environment, so activation needs
# --impure:
#
#   home-manager switch --impure --flake .#claude-web
{
  pkgs,
  webDevTools,
  sharedSkillsArgs,
  ...
}:
let
  envUser = builtins.getEnv "USER";
  envHome = builtins.getEnv "HOME";
  user = if envUser != "" then envUser else "user";

  # The reason this profile exists: deploy skills to ~/.claude/skills via the
  # shared home-manager skills module.
  mkSkills = import ../skills.nix sharedSkillsArgs;
in
{
  home.username = user;
  home.homeDirectory = if envHome != "" then envHome else "/home/${user}";
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

  home.file = mkSkills { prefix = ".claude"; };
}
