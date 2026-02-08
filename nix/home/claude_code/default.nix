# Claude Code Configuration Module
#
# Reference: Claude Code documentation as of 2026-01-20
# JSON Schema: https://json.schemastore.org/claude-code-settings.json
#
# Sources:
#   - https://code.claude.com/docs/en/settings.md
#   - https://code.claude.com/docs/en/network-config.md
#   - https://code.claude.com/docs/en/model-config.md
#
# ============================================================================
# SETTINGS.JSON OPTIONS (config.programs.claude-code.settings)
# ============================================================================
#
# General Settings:
#   theme                     : string   : UI theme ("dark", "light")
#   language                  : string   : Response language ("japanese", "spanish", etc.)
#   model                     : string   : Default model ("opus", "sonnet", "haiku", "opusplan")
#   outputStyle               : string   : Output style name (e.g., "Explanatory")
#   promptSuggestions         : boolean  : Enable prompt suggestions in UI
#   showTurnDuration          : boolean  : Show "Cooked for Xm Ys" messages (default: true)
#   spinnerTipsEnabled        : boolean  : Show tips while Claude works (default: true)
#   terminalProgressBarEnabled: boolean  : Terminal progress bar (default: true)
#   cleanupPeriodDays         : integer  : Days before session cleanup (0 = IMMEDIATE deletion, default: 30, use 9999 to disable)
#   plansDirectory            : string   : Plan file storage (default: "~/.claude/plans")
#   respectGitignore          : boolean  : File picker respects .gitignore (default: true)
#
# Attribution (git commits/PRs):
#   includeCoAuthoredBy       : boolean  : DEPRECATED - use attribution.* instead
#   attribution.commit        : string   : Git commit attribution (empty to hide)
#   attribution.pr            : string   : PR description attribution
#
# Authentication:
#   apiKeyHelper              : string   : Script path to generate auth values
#   awsAuthRefresh            : string   : Script to refresh AWS Bedrock credentials
#   awsCredentialExport       : string   : Script outputting JSON with AWS credentials
#   forceLoginMethod          : string   : Force login: "claudeai" or "console"
#   forceLoginOrgUUID         : string   : Organization UUID for auto-select
#
# Auto-updates:
#   autoUpdatesChannel        : string   : "stable" (1 week old) or "latest" (default)
#
# Status Line:
#   statusLine.type           : string   : Only "command" supported
#   statusLine.command        : string   : Script to generate status line
#
# File Suggestions:
#   fileSuggestion.type       : string   : Only "command" supported
#   fileSuggestion.command    : string   : Custom script for @ file autocomplete
#
# Sandbox (bash isolation):
#   sandbox.enabled                  : boolean : Enable bash sandboxing
#   sandbox.autoAllowBashIfSandboxed : boolean : Auto-approve sandboxed bash (default: true)
#   sandbox.allowUnsandboxedCommands : boolean : Allow dangerouslyDisableSandbox (default: true)
#   sandbox.excludedCommands         : array   : Commands that bypass sandbox (e.g., ["docker"])
#   sandbox.enableWeakerNestedSandbox: boolean : Weaker sandbox for unprivileged Docker (Linux)
#   sandbox.ignoreViolations         : object  : Command patterns to paths for violations
#   sandbox.network.allowLocalBinding: boolean : Allow localhost port binding (macOS)
#   sandbox.network.allowUnixSockets : array   : Unix socket paths accessible in sandbox
#   sandbox.network.httpProxyPort    : integer : HTTP proxy port (auto if not set)
#   sandbox.network.socksProxyPort   : integer : SOCKS5 proxy port (auto if not set)
#
# Permissions:
#   permissions.allow              : array  : Rules to allow (lowest priority)
#   permissions.ask                : array  : Rules that prompt for confirmation
#   permissions.deny               : array  : Rules to deny (highest priority)
#   permissions.defaultMode        : string : "acceptEdits", "bypassPermissions", "default", "plan"
#   permissions.additionalDirectories      : array  : Extra working directories
#   permissions.disableBypassPermissionsMode: string: Set "disable" to prevent bypass
#
# Permission Rule Syntax:
#   "Tool"                    : Match all uses of tool
#   "Tool(exact:command)"     : Exact match
#   "Tool(prefix:*)"          : Prefix with word boundary
#   "Tool(glob*)"             : Glob anywhere in string
#   "WebFetch(domain:x.com)"  : Domain filtering
#   "Read(~/.config/**)"      : Path with glob patterns
#
# MCP (Model Context Protocol):
#   disabledMcpjsonServers    : array   : MCP servers from .mcp.json to reject
#   enabledMcpjsonServers     : array   : MCP servers from .mcp.json to approve
#   enableAllProjectMcpServers: boolean : Auto-approve all project MCP servers
#
# Plugins:
#   enabledPlugins            : object  : Format: "plugin@marketplace": true/false
#
# Environment Variables (passed to sessions):
#   env                       : object  : Key-value environment variables
#
# Hooks:
#   disableAllHooks           : boolean : Disable all hooks and statusLine execution
#
# WebFetch:
#   skipWebFetchPreflight     : boolean : Skip blocklist check
#
# OpenTelemetry:
#   otelHeadersHelper         : string  : Script that outputs OTEL headers
#
# Enterprise/Managed Settings (system-level only):
#   allowManagedHooksOnly     : boolean : Only load managed + SDK hooks
#   allowedMcpServers         : array   : Allowlist of MCP servers
#   deniedMcpServers          : array   : Denylist of MCP servers
#   strictKnownMarketplaces   : array   : Allowlist of plugin marketplaces
#   companyAnnouncements      : array   : Startup announcements
#
# ============================================================================
# ENVIRONMENT VARIABLES
# ============================================================================
#
# Authentication & API:
#   ANTHROPIC_API_KEY                    : API key (X-Api-Key header)
#   ANTHROPIC_AUTH_TOKEN                 : Custom Authorization: Bearer value
#   ANTHROPIC_CUSTOM_HEADERS             : Custom headers in "Name: Value" format
#   ANTHROPIC_FOUNDRY_API_KEY            : Microsoft Foundry API key
#   AWS_BEARER_TOKEN_BEDROCK             : AWS Bedrock API key
#   CLAUDE_CODE_API_KEY_HELPER_TTL_MS    : Credential refresh interval (ms)
#   CLAUDE_CODE_OAUTH_TOKEN              : Override credentials file
#
# Model Configuration:
#   ANTHROPIC_MODEL                      : Model alias or name
#   ANTHROPIC_DEFAULT_OPUS_MODEL         : Full model name for "opus" alias
#   ANTHROPIC_DEFAULT_SONNET_MODEL       : Full model name for "sonnet" alias
#   ANTHROPIC_DEFAULT_HAIKU_MODEL        : Full model name for "haiku" alias
#   CLAUDE_CODE_SUBAGENT_MODEL           : Model for subagents
#
# Prompt Caching:
#   DISABLE_PROMPT_CACHING               : 0/1 - Disable for all models
#   DISABLE_PROMPT_CACHING_HAIKU         : 0/1 - Disable for Haiku
#   DISABLE_PROMPT_CACHING_SONNET        : 0/1 - Disable for Sonnet
#   DISABLE_PROMPT_CACHING_OPUS          : 0/1 - Disable for Opus
#
# Bash & Shell:
#   BASH_DEFAULT_TIMEOUT_MS              : Default bash timeout
#   BASH_MAX_TIMEOUT_MS                  : Maximum bash timeout
#   BASH_MAX_OUTPUT_LENGTH               : Characters before truncation
#   CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR : 0/1 - Reset to project dir after bash
#   CLAUDE_CODE_SHELL                    : Override shell detection
#   CLAUDE_CODE_SHELL_PREFIX             : Prefix to wrap all bash commands
#   CLAUDE_ENV_FILE                      : Path to env setup script
#
# Context & Performance:
#   CLAUDE_AUTOCOMPACT_PCT_OVERRIDE      : Context threshold for auto-compaction (1-100)
#   CLAUDE_CODE_MAX_OUTPUT_TOKENS        : Max output tokens (default: 32000, max: 64000)
#   CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS : Override token limit for file reads
#   MAX_THINKING_TOKENS                  : Extended thinking budget (default: 31999, 0=disable)
#   MAX_MCP_OUTPUT_TOKENS                : Max tokens in MCP responses (default: 25000)
#
# Cloud Providers:
#   CLAUDE_CODE_USE_BEDROCK              : 0/1 - Use AWS Bedrock
#   CLAUDE_CODE_SKIP_BEDROCK_AUTH        : 0/1 - Skip AWS auth
#   CLAUDE_CODE_USE_VERTEX               : 0/1 - Use Google Vertex AI
#   CLAUDE_CODE_SKIP_VERTEX_AUTH         : 0/1 - Skip Google auth
#   CLAUDE_CODE_USE_FOUNDRY              : 0/1 - Use Microsoft Foundry
#   CLAUDE_CODE_SKIP_FOUNDRY_AUTH        : 0/1 - Skip Azure auth
#   VERTEX_REGION_CLAUDE_*               : Override Vertex AI regions per model
#   ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION: Override AWS region for Haiku
#
# Network & Proxy:
#   HTTP_PROXY                           : HTTP proxy server
#   HTTPS_PROXY                          : HTTPS proxy server (preferred)
#   NO_PROXY                             : Domains/IPs to bypass proxy
#   NODE_EXTRA_CA_CERTS                  : Path to custom CA certificate
#   CLAUDE_CODE_PROXY_RESOLVES_HOSTS     : true/false - Proxy does DNS
#
# mTLS:
#   CLAUDE_CODE_CLIENT_CERT              : Path to client certificate (.pem)
#   CLAUDE_CODE_CLIENT_KEY               : Path to client private key (.pem)
#   CLAUDE_CODE_CLIENT_KEY_PASSPHRASE    : Passphrase for encrypted key
#
# MCP & Tools:
#   MCP_TIMEOUT                          : MCP server startup timeout (ms)
#   MCP_TOOL_TIMEOUT                     : MCP tool execution timeout (ms)
#   ENABLE_TOOL_SEARCH                   : "auto", "auto:N", "true", "false"
#   SLASH_COMMAND_TOOL_CHAR_BUDGET       : Max chars for skill metadata (default: 15000)
#
# Telemetry & Reporting:
#   DISABLE_TELEMETRY                    : 0/1 - Opt out of Statsig
#   DISABLE_ERROR_REPORTING              : 0/1 - Opt out of Sentry
#   DISABLE_BUG_COMMAND                  : 0/1 - Disable /bug command
#   DISABLE_COST_WARNINGS                : 0/1 - Disable cost warnings
#   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC : 0/1 - Disable autoupdater, bug, telemetry
#   DISABLE_NON_ESSENTIAL_MODEL_CALLS    : 0/1 - Disable flavor text model calls
#
# OpenTelemetry:
#   CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS : Refresh interval (default: 1740000)
#   CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS : 0/1 - Disable anthropic-beta headers
#
# Updates & Features:
#   DISABLE_AUTOUPDATER                  : 0/1 - Disable auto-updates
#   FORCE_AUTOUPDATE_PLUGINS             : true/false - Force plugin updates
#   CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL    : 0/1 - Skip IDE extension install
#
# UI & Display:
#   CLAUDE_CODE_HIDE_ACCOUNT_INFO        : 0/1 - Hide email/org (for streaming)
#   CLAUDE_CODE_DISABLE_TERMINAL_TITLE   : 0/1 - Disable terminal title updates
#   IS_DEMO                              : true/false - Demo mode
#
# Background Tasks:
#   CLAUDE_CODE_DISABLE_BACKGROUND_TASKS : 0/1 - Disable Ctrl+B, subagent backgrounds
#   CLAUDE_CODE_EXIT_AFTER_STOP_DELAY    : ms to wait before auto-exit (SDK mode)
#
# Configuration & Storage:
#   CLAUDE_CONFIG_DIR                    : Override config directory (default: ~/.claude)
#   CLAUDE_CODE_TMPDIR                   : Override temp directory
#   USE_BUILTIN_RIPGREP                  : 0/1 - Use system rg instead of bundled
#
# ============================================================================
{
  config,
  pkgs,
  pkgsUnstable,
  lib,
  ...
}:
let
  cfg = config.programs.claude-code;

  # Gmail MCP Server - pinned to specific commit for security
  gmail-mcp-server = import ../packages/gmail-mcp.nix { inherit pkgs lib; };

  # Helper to generate Read/Grep/Glob permissions for directories
  # Allows recursive access to all files in specified directories
  # Pattern syntax: https://code.claude.com/docs/en/settings
  #   - Supports glob patterns: ** for recursive, * for wildcard
  #   - Supports ~ for home directory expansion
  mkReadPerms =
    dirs:
    lib.flatten (
      map (
        dir:
        map (tool: "${tool}(${dir}/**)") [
          "Read"
          "Grep"
          "Glob"
        ]
      ) dirs
    );

  # Directories where Read/Grep/Glob are always allowed without prompting
  alwaysAllowedReadDirs = [
    "~/.claude" # Claude Code session history, settings, commands
  ]
  ++ cfg.extraAllowedReadDirs;

  # Helper to generate WebFetch domain permissions
  mkWebFetchDomainPerms = domains: map (domain: "WebFetch(domain:${domain})") domains;

  # Domains where WebFetch is always allowed
  allowedWebFetchDomains = [
    "pypi.org"
    "docs.python.org"
    "json.schemastore.org"
    "www.schemastore.org"
    "code.claude.com" # Claude Code documentation
  ]
  ++ cfg.extraAllowedWebFetchDomains;

  # Extra working directories (full read/write access, extends beyond CWD)
  # /code contains all git repos organized by host (github.com, gitlab.com, etc.)
  baseAdditionalDirs = [
    "/code"
    "~/.cache/pre-commit"
  ]
  ++ cfg.additionalDirectories;

  # System inspection command permissions (auto-allow for read-only commands)
  inspectionPerms = import ./inspection-permissions.nix { inherit lib; };

  # Auto-discover all .md files in commands/ directory
  commandsDir = ./commands;
  commandFiles = builtins.readDir commandsDir;
  commands = lib.mapAttrs' (
    name: type: lib.nameValuePair (lib.removeSuffix ".md" name) (commandsDir + "/${name}")
  ) (lib.filterAttrs (name: type: type == "regular" && lib.hasSuffix ".md" name) commandFiles);

  # Skills directory for Claude Code
  # Skills are model-invoked capabilities that Claude automatically uses based on context
  # Each skill is a subdirectory containing SKILL.md and optional supporting files
  skillsDir = ./skills;
in
{
  options.programs.claude-code.extraAllowedReadDirs = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    description = "Additional directories to auto-allow for Read/Grep/Glob operations";
    example = [ "/wyrmhdd/bazel" ];
  };

  options.programs.claude-code.additionalDirectories = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    description = "Additional directories to include in the permission scope (extends beyond working directory)";
    example = [
      "/tmp"
      "/mnt/data"
    ];
  };

  options.programs.claude-code.extraAllowedWebFetchDomains = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    description = "Additional domains to auto-allow for WebFetch operations";
    example = [
      "docs.rs"
      "crates.io"
    ];
  };

  config.programs.claude-code = {
    enable = true;
    package = pkgsUnstable.claude-code; # Use unstable for faster updates

    commands = commands;

    mcpServers = {
      tana-local = {
        type = "http";
        url = "http://localhost:8262/mcp";
      };

      # Gmail integration via MCP
      # Setup: See nix/home/packages/gmail-mcp.nix for full instructions
      # Quick start: gmail-mcp-auth (after configuring Google Cloud OAuth)
      gmail = {
        type = "stdio";
        command = "${gmail-mcp-server}/bin/gmail-mcp";
        args = [ ];
      };
    };

    settings = {
      theme = "dark";
      attribution.commit = ""; # Disable "Co-authored-by" in commits
      attribution.pr = ""; # Disable attribution in PR descriptions
      # 9999 = effectively disable cleanup (0 = delete immediately, which is wrong)
      cleanupPeriodDays = 9999;
      promptSuggestions = true;
      statusLine = {
        type = "command";
        command = "/home/agentydragon/.claude/statusline.py";
      };

      sandbox = {
        enabled = true;
        autoAllowBashIfSandboxed = true;
        allowUnsandboxedCommands = true;
        excludedCommands = [ "nvidia-smi" ];
      };

      permissions = {
        allow = [
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
        ++ mkWebFetchDomainPerms allowedWebFetchDomains
        ++ mkReadPerms alwaysAllowedReadDirs
        ++ inspectionPerms.permissions;
        deny = [ ];
        defaultMode = "default";
        additionalDirectories = baseAdditionalDirs;
      };
    };
  };

  # Add gmail-mcp-server to PATH for auth setup command
  config.home.packages = [ gmail-mcp-server ];

  # Deploy skills to ~/.claude/skills/
  # Skills are stored in nix/home/claude_code/skills/ and symlinked for declarative management
  config.home.file = {
    ".claude/statusline.py" = {
      source = ./statusline.py;
      executable = true;
    };
  }
  // lib.mapAttrs' (
    skillName: skillType:
    lib.nameValuePair ".claude/skills/${skillName}" {
      source = skillsDir + "/${skillName}";
      recursive = true;
    }
  ) (lib.filterAttrs (name: type: type == "directory") (builtins.readDir skillsDir));
}
