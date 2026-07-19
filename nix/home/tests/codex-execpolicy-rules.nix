# Test Codex execpolicy rule generation from the shared allowed-commands SSOT.
#
# Run: nix-instantiate --eval --strict nix/home/tests/codex-execpolicy-rules.nix

let
  pkgs = import <nixpkgs> { };
  inherit (pkgs) lib;

  allowed = import ../allowed-commands.nix;
  generated = import ../codex/execpolicy-rules.nix { inherit lib; };

  expectedPrefixRules =
    command: subcommands:
    map (
      subcommand:
      let
        pattern = lib.concatMapStringsSep "," builtins.toJSON (
          [ command ] ++ lib.splitString " " subcommand
        );
      in
      "prefix_rule(pattern=[${pattern}], decision=\"allow\")"
    ) subcommands;

  expectedGitReadOnlyRules = expectedPrefixRules "git" [
    "diff"
    "log"
    "show"
    "stash list"
    "stash show"
    "status"
  ];

  expectedProductRules =
    commands: subcommands:
    builtins.concatMap (command: expectedPrefixRules command subcommands) commands;

  expectedBazelRules =
    expectedProductRules
      [
        "bazel"
        "bazelisk"
      ]
      [
        "query"
        "cquery"
        "aquery"
        "info"
        "build"
        "test"
      ];

  expectedNixRules = expectedPrefixRules "nix" [
    "eval"
    "build"
  ];
in
{
  test_rule_count_matches_prefix_entries = {
    expr = builtins.length generated.rules;
    expected = builtins.length (builtins.filter (entry: entry.type == "prefix") allowed.noSudo);
  };

  test_has_git_read_only_rules = {
    expr = builtins.all (rule: builtins.elem rule generated.rules) expectedGitReadOnlyRules;
    expected = true;
  };

  test_has_gh_run_view_rule = {
    expr = builtins.elem "prefix_rule(pattern=[\"gh\",\"run\",\"view\"], decision=\"allow\")" generated.rules;
    expected = true;
  };

  test_has_bazel_rules = {
    expr = builtins.all (rule: builtins.elem rule generated.rules) expectedBazelRules;
    expected = true;
  };

  test_has_bazelisk_query_rule = {
    expr = builtins.elem "prefix_rule(pattern=[\"bazelisk\",\"query\"], decision=\"allow\")" generated.rules;
    expected = true;
  };

  test_has_nix_rules = {
    expr = builtins.all (rule: builtins.elem rule generated.rules) expectedNixRules;
    expected = true;
  };

  test_has_nix_eval_starlark_rule_line = {
    expr = lib.hasInfix "\nprefix_rule(pattern=[\"nix\",\"eval\"], decision=\"allow\")\n" generated.text;
    expected = true;
  };

  test_has_nix_build_starlark_rule_line = {
    expr = lib.hasInfix "\nprefix_rule(pattern=[\"nix\",\"build\"], decision=\"allow\")\n" generated.text;
    expected = true;
  };

  test_has_cargo_info_rule = {
    expr = builtins.elem "prefix_rule(pattern=[\"cargo\",\"info\"], decision=\"allow\")" generated.rules;
    expected = true;
  };

  test_has_cargo_search_rule = {
    expr = builtins.elem "prefix_rule(pattern=[\"cargo\",\"search\"], decision=\"allow\")" generated.rules;
    expected = true;
  };

  test_has_cargo_tree_rule = {
    expr = builtins.elem "prefix_rule(pattern=[\"cargo\",\"tree\"], decision=\"allow\")" generated.rules;
    expected = true;
  };

  test_has_header_pointer_to_checker = {
    expr = lib.hasInfix ''--rules "$CODEX_HOME/rules/managed.rules"'' generated.text;
    expected = true;
  };

  test_generated_file_identifies_ownership = {
    expr =
      lib.hasInfix "# Managed by Home Manager" generated.text
      && lib.hasInfix "$CODEX_HOME/rules/default.rules" generated.text;
    expected = true;
  };
}
