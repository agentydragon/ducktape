{
  pkgs,
  lib,
  config,
  inputs,
  ...
}: {
  # Core utilities inside the shell
  packages = [
    pkgs.git
  ];

  # Python environment managed by devenv + uv
  languages.python = {
    enable = true;
    package = pkgs.python312;
    uv = {
      enable = true;
      sync = {
        enable = true;
        extras = ["dev"];
      };
    };
  };

  enterShell = ''
    set -euo pipefail
    uv sync --extra dev --quiet
    python --version
    echo "git-diff-tree devenv ready. Use 'uv run pytest' to run tests."
  '';
}
