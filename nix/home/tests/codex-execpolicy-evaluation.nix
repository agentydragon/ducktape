{
  pkgs,
  codex,
}:

let
  generated = import ../codex/execpolicy-rules.nix { inherit (pkgs) lib; };
  rulesFile = pkgs.writeText "codex-managed.rules" generated.text;
in
pkgs.runCommand "codex-execpolicy-evaluation"
  {
    nativeBuildInputs = [
      codex
      pkgs.jq
    ];
  }
  ''
    # Keep this check independent of the invoking user's Codex configuration.
    # The only policy under evaluation must be the rules generated above.
    export HOME="$TMPDIR/home"
    export CODEX_HOME="$TMPDIR/codex-home"
    mkdir -p "$HOME" "$CODEX_HOME"

    check_policy() {
      expected="$1"
      shift

      result=$(codex execpolicy check --rules '${rulesFile}' -- "$@")
      if ! printf '%s\n' "$result" | jq -e "$expected" >/dev/null; then
        echo "Codex execpolicy failed for command '$*': $result" >&2
        return 1
      fi
    }

    # Exercise a real multi-token prefix generated from allowed-commands.nix.
    check_policy \
      '.decision == "allow" and (.matchedRules | length) > 0' \
      git status --short

    # Exact entries are deliberately omitted rather than widened to prefixes.
    check_policy \
      '.matchedRules == []' \
      pwd

    check_policy \
      '(.decision == null) and (.matchedRules == [])' \
      codex-execpolicy-unlisted-command

    touch "$out"
  ''
