{
  config,
  pkgs,
  lib,
  ...
}: let
  # Helper to generate Read/Grep/Glob permissions for directories
  # Allows recursive access to all files in specified directories
  # Pattern syntax: https://code.claude.com/docs/en/settings
  #   - Supports glob patterns: ** for recursive, * for wildcard
  #   - Supports ~ for home directory expansion
  mkReadPerms = dirs:
    lib.flatten (map (
        dir:
          map (tool: "${tool}(${dir}/**)") ["Read" "Grep" "Glob"]
      )
      dirs);

  # Directories where Read/Grep/Glob are always allowed without prompting
  # /code contains all git repos organized by host (github.com, gitlab.com, etc.)
  # ~/code contains symlinks to specific projects within /code, plus some direct subdirs
  alwaysAllowedReadDirs = [
    "~/.claude" # Claude Code session history, settings, commands
    "/code" # Primary code location (canonical git repos by host)
    "/home/agentydragon/code" # Convenience symlinks + some direct projects
  ];

  # System inspection command permissions (auto-allow for read-only commands)
  inspectionPerms = import ./inspection-permissions.nix {inherit lib;};

  # Auto-discover all .md files in commands/ directory
  commandsDir = ./commands;
  commandFiles = builtins.readDir commandsDir;
  commands =
    lib.mapAttrs' (
      name: type:
        lib.nameValuePair
        (lib.removeSuffix ".md" name)
        (commandsDir + "/${name}")
    ) (lib.filterAttrs (
        name: type:
          type == "regular" && lib.hasSuffix ".md" name
      )
      commandFiles);

  # Skills directory for Claude Code
  # Skills are model-invoked capabilities that Claude automatically uses based on context
  # Each skill is a subdirectory containing SKILL.md and optional supporting files
  skillsDir = ./skills;
in {
  programs.claude-code = {
    enable = true;
    package = pkgs.claude-code;

    commands = commands;

    settings = {
      theme = "dark";
      includeCoAuthoredBy = false;
      cleanupPeriodDays = 0; # Disable transcript cleanup (retain indefinitely)
      statusLine = {
        type = "command";
        command = "/home/agentydragon/.claude/statusline.py";
      };

      # Sandbox configuration for bash commands
      # Uses Bubblewrap (Linux) or Seatbelt (macOS) for OS-level isolation.
      #
      # When enabled:
      # - Filesystem writes restricted to CWD and subdirs
      # - Network filtered through proxy (domain-based allowlist)
      # - Claude's system prompt instructs it to run commands sandboxed by default
      #
      # Escape hatch (dangerouslyDisableSandbox parameter):
      # - Claude can set dangerouslyDisableSandbox: true on Bash tool calls
      # - When set, command bypasses sandbox and goes through normal permission flow
      # - User gets prompted unless command matches an allow rule
      # - Can be disabled entirely with allowUnsandboxedCommands = false
      #
      # See: https://docs.anthropic.com/en/docs/claude-code/security#sandboxing
      sandbox = {
        enabled = true;
        # Auto-allow sandboxed commands without prompting (non-sandboxed still prompt)
        autoAllowBashIfSandboxed = true;
        # Allow Claude to use dangerouslyDisableSandbox (triggers normal approval flow)
        allowUnsandboxedCommands = true;
        # Commands that cannot run in sandbox (e.g., need privileged access)
        # excludedCommands = ["docker" "podman"];
        # Network options (if needed):
        # network.allowUnixSockets = ["/run/user/1000/docker.sock"];  # Allow specific sockets
        # network.allowLocalBinding = true;  # Allow localhost port binding (macOS)
      };

      permissions = {
        allow =
          [
            "Read"
            "Edit"
            "Write"
            "MultiEdit"
            "Search"
            "Task"
            "Bash(git status:*)"
            "Bash(git diff:*)"
            "Bash(git stash show:*)"
            "Bash(git stash list:*)"
            "WebFetch"
            "WebSearch"
          ]
          ++ mkReadPerms alwaysAllowedReadDirs
          ++ inspectionPerms.permissions;
        # ask = ["Bash(*)"];  - use Bash without parens to allow all commands
        deny = [];
        defaultMode = "default";
      };
    };
  };

  # Deploy skills to ~/.claude/skills/
  # Skills are stored in nix/home/claude-code/skills/ and symlinked for declarative management
  home.file =
    {
      ".claude/statusline.py" = {
        source = ./statusline.py;
        executable = true;
      };
    }
    // lib.mapAttrs' (
      skillName: skillType:
        lib.nameValuePair
        ".claude/skills/${skillName}"
        {
          source = skillsDir + "/${skillName}";
          recursive = true;
        }
    ) (lib.filterAttrs (
        name: type:
          type == "directory"
      )
      (builtins.readDir skillsDir));
}
