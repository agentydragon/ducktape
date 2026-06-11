# Personal Gemini CLI integration
#
# Transforms SSOTs (inspection-commands.nix, allowed-commands.nix) into
# Gemini CLI policy structures and wires them into programs.gemini-cli.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  # Import SSOTs
  inspection = import ../lib/inspection-commands.nix { inherit lib; };
  allowed = import ./allowed-commands.nix;

  # Reuse existing gmail-mcp-server package
  gmail-mcp-server = import ../packages/gmail-mcp.nix { inherit pkgs lib; };

  # Transform simple { type, cmd } → Gemini policy rule
  # Input: priority (int), entry ({ type = "prefix"|"exact"; cmd = "command string"; })
  # Output: Gemini policy rule structure
  toGeminiRule = priority: entry: {
    toolName = "run_shell_command";
    commandPrefix = entry.cmd; # Just use the command string directly!
    decision = "allow";
    inherit priority;
  };

  # Generate policy rules from SSOTs
  # Inspection commands: priority 350
  inspectionRules = map (toGeminiRule 350) (inspection.exports.noSudo ++ inspection.exports.sudo);

  # Allowed commands: priority 300
  allowedRules = map (toGeminiRule 300) allowed.noSudo;
in
{
  programs.gemini-cli = {
    enable = true;
    policies = {
      inspection-commands = inspectionRules;
      allowed-commands = allowedRules;
    };

    settings = {
      security = {
        auth = {
          selectedType = "oauth-personal";
        };
      };

      # Personal settings overrides
      general = {
        preferredEditor = "nvim";
        enableAutoUpdate = false; # Nix manages package updates
        enablePromptCompletion = true; # AI-powered prompt suggestions
      };

      ui = {
        showMemoryUsage = true;
        inlineThinkingMode = "full";
        showLineNumbers = true;
        showStatusInTitle = true; # Show model thoughts in window title
        showModelInfoInChat = true; # Show model names in chat turns
        loadingPhrases = "off"; # Disable loading phrases
        hideBanner = true;
      };

      footer = {
        hideContextPercentage = false; # Show remaining context percentage
      };

      context = {
        fileName = [
          "AGENTS.md"
          "GEMINI.md"
        ];
        includeDirectories = [
          "/code"
          "~/.cache/pre-commit"
          "~/.config/gemini"
        ];
        loadMemoryFromIncludeDirectories = true;
        includeDirectoryTree = true;
        fileFiltering = {
          respectGitIgnore = true;
          respectGeminiIgnore = true;
          enableRecursiveFileSearch = true;
          enableFuzzySearch = true;
        };
      };

      tools = {
        sandbox = true;
        allowed = [
          "read_file"
          "list_directory"
          "search_files"
        ];
        shell = {
          showColor = true;
          enableInteractiveShell = true;
        };
        useRipgrep = true;

        # NOTE: Advanced tool customization options to explore:
        # - discoveryCommand: Custom command to discover available tools (alternative to built-in discovery)
        # - callCommand: Custom command to invoke tools (allows wrapping/interception)
      };

      model = {
        compressionThreshold = 0.9;
        maxSessionTurns = -1;
      };

      # TODO: Consider advanced.excludedEnvVars to exclude noisy env vars from context
      # Default: ["DEBUG" "DEBUG_MODE"]

      experimental = {
        enableAgents = true;
        plan = true; # Enable plan mode for architecting complex changes
        extensionManagement = true; # Extension management UI
        extensionRegistry = true; # Extension registry UI for discovering extensions
        extensionConfig = true; # Allow extensions to have their own settings
        modelSteering = true; # User guidance hints to the model

        # TODOs: Features to consider enabling
        # TODO: jitContext - Just-in-time context loading for large repos (reduces initial load)
        # TODO: extensionReloading - Reload extensions without restarting session
        # TODO: useOSC52Paste/useOSC52Copy - Remote terminal clipboard integration
      };

      skills = {
        enabled = true;
        disabled = [ ];
      };

      hooksConfig = {
        enabled = true;
        disabled = [ ];
        notifications = true;
      };

      # MCP servers
      mcpServers = {
        gmail = {
          command = "${gmail-mcp-server}/bin/gmail-mcp";
          args = [ ];
        };
      };
    };
  };

  # Add gmail-mcp-server to PATH for auth setup
  home.packages = [ gmail-mcp-server ];
}
