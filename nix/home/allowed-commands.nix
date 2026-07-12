# Single Source of Truth for AI agent always-allowed commands
#
# Used by:
#   - nix/home/claude_code/default.nix (Claude Code permissions)
#   - nix/home/codex/execpolicy-rules.nix (Codex execpolicy)
#   - nix/home/gemini_cli.nix (Gemini CLI policies)
#
# Commands listed here are safe for AI agents to execute without user approval.
#
# Safety criteria:
#   ✓ Read-only operations (query state, don't modify)
#   ✓ May write to build artifacts (bazel-out/, target/) but not source
#   ✓ No side effects on system state
#   ✗ Don't modify source code (git add/commit, file edits)
#   ✗ Don't modify system state (package installs, service control)
#
# Format: { type = "prefix"|"exact"; cmd = "full command string"; }
#   - type = "prefix": allows trailing arguments (e.g., "git status --short")
#   - type = "exact": no additional arguments allowed
let
  prefixCommandProduct =
    commands: subcommands:
    builtins.concatMap (
      command:
      map (subcommand: {
        type = "prefix";
        cmd = "${command} ${subcommand}";
      }) subcommands
    ) commands;

  gitReadOnlyCommands =
    prefixCommandProduct
      [ "git" ]
      [
        "diff"
        "log"
        "show"
        "stash list"
        "stash show"
        "status"
      ];

  ghReadOnlyCommands =
    prefixCommandProduct
      [ "gh" ]
      [
        "pr checks"
        "pr view"
      ];

  bazelExecutables = [
    "bazel"
    "bazelisk"
  ];

  bazelSubcommands = [
    "query"
    "cquery"
    "aquery"
    "info"
    "build"
    "test"
  ];

  bazelCommands = prefixCommandProduct bazelExecutables bazelSubcommands;

  # Generate "nix develop <flag> <cmd>" variants for both --command and -c flags.
  nixDevelopWrapped =
    commands:
    let
      commandFlags = [
        "--command"
        "-c"
      ];
    in
    builtins.concatMap (cmd: map (flag: "nix develop ${flag} ${cmd}") commandFlags) commands;

  nixDevelopBazelCommands =
    let
      nixDevelopBazel = nixDevelopWrapped bazelExecutables;
    in
    prefixCommandProduct nixDevelopBazel bazelSubcommands;

  # Commands that need both bare and nix-develop-wrapped variants.
  # bareOnlyWrapped: no subcommand, just the command with trailing args
  # bareAndWrapped: command + subcommand (e.g. "pre-commit run")
  wrappedCommands =
    let
      bareAndWrapped =
        commands: subcommands:
        let
          bare = prefixCommandProduct commands subcommands;
          wrapped = prefixCommandProduct (nixDevelopWrapped commands) subcommands;
        in
        bare ++ wrapped;

      bareOnlyWrapped =
        commands:
        let
          bare = map (cmd: {
            type = "prefix";
            inherit cmd;
          }) commands;
          wrapped = map (cmd: {
            type = "prefix";
            inherit cmd;
          }) (nixDevelopWrapped commands);
        in
        bare ++ wrapped;
    in
    bareOnlyWrapped [ "prettier" ]
    ++ bareAndWrapped [ "pre-commit" ] [ "run" ]
    ++ bareAndWrapped [ "talosctl" ] [ "version" ];

  nixCommands =
    prefixCommandProduct
      [ "nix" ]
      [
        "eval"
        "build"
        "hash"
      ];

  cargoMetadataCommands =
    prefixCommandProduct
      [ "cargo" ]
      [
        "info"
        "search"
        "tree"
      ];

  # bbapi — BuildBuddy API CLI. Almost entirely read-only (invocations,
  # targets, artifacts, logs, cache scorecard, trends, executions, AI
  # analysis); the one side-effecting subcommand is `workflow run`, which
  # triggers a CI workflow execution. Needs network (app.buildbuddy.io), so
  # it must run outside the sandbox — the prefix allow rule is treated as
  # sandbox-bypassing by both Claude Code and Codex execpolicy.
  bbapiCommands = [
    {
      type = "prefix";
      cmd = "bbapi";
    }
  ];
in
{
  # All allowed commands (no sudo - these are user-accessible commands)
  noSudo =
    gitReadOnlyCommands
    ++ ghReadOnlyCommands
    ++ bazelCommands
    ++ nixDevelopBazelCommands
    ++ nixCommands
    ++ [
      # TODO: Add more git read-only commands:
      # { type = "prefix"; cmd = "git branch"; }
      # { type = "prefix"; cmd = "git remote"; }
      # { type = "prefix"; cmd = "git tag"; }
      # { type = "prefix"; cmd = "git blame"; }
      # { type = "prefix"; cmd = "git reflog"; }

      # Home manager operations
      {
        type = "prefix";
        cmd = "home-manager build";
      }

      # Nix prefetch (read-only — fetches and prints hash without adding to store)
      {
        type = "prefix";
        cmd = "nix-prefetch-url";
      }
      {
        type = "exact";
        cmd = "pwd";
      }
    ]
    ++ wrappedCommands
    ++ cargoMetadataCommands
    ++ bbapiCommands;

  # TODO: Add build system queries:
  # { type = "prefix"; cmd = "bazelisk fetch"; }
  # { type = "prefix"; cmd = "npm list"; }
  # { type = "prefix"; cmd = "npm outdated"; }
  # { type = "prefix"; cmd = "npm audit"; }
  # { type = "prefix"; cmd = "pip list"; }
  # { type = "prefix"; cmd = "pip show"; }

  # TODO: Add test execution commands (safe - only writes to build artifacts):
  # { type = "exact"; cmd = "npm test"; }
  # { type = "exact"; cmd = "npm run test"; }
  # { type = "exact"; cmd = "cargo test"; }
  # { type = "exact"; cmd = "cargo check"; }
  # { type = "exact"; cmd = "cargo clippy"; }
}
