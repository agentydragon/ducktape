# Generate Codex execpolicy rules from the shared allowed-commands SSOT.
#
# Codex execpolicy is prefix-based. `exact` entries from the SSOT are skipped
# rather than widened into prefix rules — Codex still falls back to its
# built-in trusted-command heuristic for those commands.
#
# TODO: emit exact-match rules for `exact` entries when codex-execpolicy
# grows native support for them (e.g. an `exact_rule(...)` primitive). For
# now, `pwd` and friends just fall through to the trusted-command heuristic.
{ lib }:
let
  allowed = import ../allowed-commands.nix;

  tokenize = cmd: builtins.filter (part: part != "") (lib.splitString " " cmd);

  prefixEntries = builtins.filter (entry: entry.type == "prefix") allowed.noSudo;

  renderRule =
    entry: "prefix_rule(pattern=${builtins.toJSON (tokenize entry.cmd)}, decision=\"allow\")";

  header = ''
    # Auto-generated from nix/home/allowed-commands.nix.
    # Codex loads *.rules from $CODEX_HOME/rules/ automatically.
    #
    # Syntax quickstart:
    #   prefix_rule(pattern=["git", "status"], decision="allow")
    #   prefix_rule(pattern=["git", "commit"], decision="prompt", justification="history-changing")
    #   prefix_rule(pattern=["rm"], decision="forbidden", justification="destructive; use a safer alternative")
    #
    # Test this file locally:
    #   codex-execpolicy check --pretty --rules "$CODEX_HOME/rules/default.rules" -- git status
    #   codex-execpolicy check --pretty --rules "$CODEX_HOME/rules/default.rules" -- bash -lc 'git status'
    #
    # Codex treats matching `decision="allow"` rules as sandbox-bypassing for
    # the matched command prefix, so keep this file limited to safe commands.
    #
    # `match=` / `not_match=` are load-time examples, not exact-match enforcement.
    # This generator only emits allow rules for shared `prefix` commands.
  '';

  rules = map renderRule prefixEntries;
in
{
  inherit rules;
  text = lib.concatLines ([ header ] ++ rules);
}
