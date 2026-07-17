# Claude Code Configuration Module
#
# Reference: Claude Code documentation as of 2026-05-26
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
#   model                     : string   : Default model:
#                                          "default"    - account-tier default (clears override)
#                                          "best"       - most capable available (currently Opus)
#                                          "opus"       - latest Opus
#                                          "sonnet"     - latest Sonnet
#                                          "haiku"      - fast Haiku
#                                          "opus[1m]"   - Opus with 1M context window
#                                          "sonnet[1m]" - Sonnet with 1M context window
#                                          "opusplan"   - Opus for planning, Sonnet for execution
#                                          full model ID (e.g. "claude-sonnet-4-6") also works
#   effortLevel               : string   : Persist effort across sessions ("low","medium","high","xhigh")
#   availableModels           : array    : Restrict model selection (enterprise)
#   modelOverrides            : object   : Map Anthropic IDs to provider-specific IDs (Bedrock/Vertex/Foundry)
#   outputStyle               : string   : Output style name (e.g., "Explanatory")
#   promptSuggestions         : boolean  : Enable prompt suggestions in UI
#   showTurnDuration          : boolean  : Show "Cooked for Xm Ys" messages (default: true)
#   spinnerTipsEnabled        : boolean  : Show tips while Claude works (default: true)
#   terminalProgressBarEnabled: boolean  : Terminal progress bar (default: true)
#   cleanupPeriodDays         : integer  : Days before session cleanup (0 = IMMEDIATE deletion, default: 30, use 9999 to disable)
#   plansDirectory            : string   : Plan file storage (default: "~/.claude/plans")
#   respectGitignore          : boolean  : File picker respects .gitignore (default: true)
#   editorMode                : string   : Input key binding mode ("normal" or "vim")
#   prefersReducedMotion      : boolean  : Reduce animations (accessibility)
#   syntaxHighlightingDisabled: boolean  : Disable syntax highlighting
#   autoScrollEnabled         : boolean  : Follow new output in fullscreen mode (default: true)
#   awaySummaryEnabled        : boolean  : Show recap when returning to terminal (default: true)
#   preferredNotifChannel     : string   : Task completion notifications: "auto", "terminal_bell",
#                                          "iterm2", "iterm2_with_bell", "kitty", "ghostty",
#                                          "notifications_disabled"
#   teammateMode              : string   : Agent team display mode ("in-process", "auto", "tmux")
#   spinnerVerbs              : object   : Customize action verbs {mode: "append", verbs: [...]}
#   showClearContextOnPlanAccept: boolean: Show context clearing on plan accept (default: false)
#   prUrlTemplate             : string   : PR URL template for code-review tools
#   feedbackSurveyRate        : number   : Survey probability (0-1)
#
# Auto Mode:
#   disableAutoMode           : string   : Set "disable" to prevent auto mode (remove from Shift+Tab cycle)
#   autoMode                  : object   : Customize auto mode classifier:
#                                          {environment: [...], allow: [...], soft_deny: [...], hard_deny: [...]}
#                                          Include "$defaults" in an array to inherit built-in rules
#   useAutoModeDuringPlan     : boolean  : Use auto mode semantics in plan mode (default: true)
#   NOTE: defaultMode "auto" is ignored in project/local settings (managed settings only since v2.1.142)
#   NOTE: "$defaults" content is not publicly documented; "claude auto-mode" subcommand exists
#         in help but sub-subcommands (defaults/config/critique) may not work in all versions
#
# Extended Thinking:
#   alwaysThinkingEnabled     : boolean  : Enable extended thinking by default
#   showThinkingSummaries     : boolean  : Display thinking summaries in interactive sessions (default: false)
#
# Auto Memory:
#   autoMemoryEnabled         : boolean  : Enable auto memory (default: true)
#   autoMemoryDirectory       : string   : Custom auto memory storage location
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
#   sandbox.enabled                   : boolean : Enable bash sandboxing
#   sandbox.autoAllowBashIfSandboxed  : boolean : Auto-approve sandboxed bash (default: true)
#   sandbox.allowUnsandboxedCommands  : boolean : Allow dangerouslyDisableSandbox (default: true)
#   sandbox.excludedCommands          : array   : Commands that bypass sandbox (e.g., ["docker"])
#   sandbox.enableWeakerNestedSandbox : boolean : Weaker sandbox for unprivileged Docker (Linux)
#   sandbox.ignoreViolations          : object  : Command patterns to paths for violations
#   sandbox.failIfUnavailable         : boolean : Fail if sandbox cannot be activated
#   sandbox.filesystem.allowWrite     : array   : Extra writable paths
#   sandbox.filesystem.denyWrite      : array   : Deny writes (takes priority over allowWrite)
#   sandbox.filesystem.denyRead       : array   : Deny reads
#   sandbox.network.allowLocalBinding : boolean : Allow localhost port binding (macOS)
#   sandbox.network.allowUnixSockets  : array   : Unix socket paths accessible in sandbox
#   sandbox.network.allowedDomains    : array   : Allow-listed outbound domains
#   sandbox.network.deniedDomains     : array   : Deny-listed outbound domains
#   sandbox.network.allowManagedDomainsOnly: boolean : Only managed domain rules apply
#   sandbox.network.httpProxyPort     : integer : HTTP proxy port (auto if not set)
#   sandbox.network.socksProxyPort    : integer : SOCKS5 proxy port (auto if not set)
#
# Permissions:
#   permissions.allow                     : array  : Rules to allow (lowest priority)
#   permissions.ask                       : array  : Rules that prompt for confirmation
#   permissions.deny                      : array  : Rules to deny (highest priority)
#   permissions.defaultMode               : string : "acceptEdits", "bypassPermissions", "default", "plan"
#                                                    ("auto" was removed from user settings in v2.1.142)
#   permissions.additionalDirectories     : array  : Extra working directories
#   permissions.disableBypassPermissionsMode: string: Set "disable" to prevent bypass
#   permissions.skipDangerousModePermissionPrompt: boolean: Skip --dangerously-skip-permissions confirm
#   permissions.allowManagedPermissionRulesOnly: boolean: Only managed settings' permission rules apply
#   allowedHttpHookUrls               : array  : HTTP hook URL allowlist
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
# Skills/Commands:
#   skillOverrides            : object  : Per-skill visibility: "on","name-only","user-invocable-only","off"
#   maxSkillDescriptionChars  : integer : Skill description char limit (default: 1536)
#   skillListingBudgetFraction: number  : Context allocation for skill listing (default: 0.01)
#
# Plugins:
#   enabledPlugins            : object  : Format: "plugin@marketplace": true/false
#
# Worktree:
#   worktree.bgIsolation      : string  : Background session isolation ("worktree" or "none")
#   worktree.baseRef          : string  : Base ref for worktree creation
#   worktree.symlinkDirectories: array  : Dirs to symlink into worktrees
#   worktree.sparsePaths      : array   : Sparse-checkout paths for worktrees
#
# Environment Variables (passed to sessions):
#   env                       : object  : Key-value environment variables
#
# Hooks:
#   disableAllHooks           : boolean : Disable all hooks and statusLine execution
#   allowedHttpHookUrls       : array   : HTTP hook URL allowlist
#
# WebFetch:
#   skipWebFetchPreflight     : boolean : Skip blocklist check
#
# OpenTelemetry:
#   otelHeadersHelper         : string  : Script that outputs OTEL headers
#
# Enterprise/Managed Settings (system-level only):
#   allowManagedHooksOnly     : boolean : Only load managed + SDK hooks
#   allowManagedMcpServersOnly: boolean : Only admin-defined MCP servers allowed
#   allowedMcpServers         : array   : Allowlist of MCP servers
#   deniedMcpServers          : array   : Denylist of MCP servers
#   strictKnownMarketplaces   : array   : Allowlist of plugin marketplaces
#   blockedMarketplaces       : array   : Denylist of marketplaces
#   allowedChannelPlugins     : array   : Allowlist of channel plugins
#   pluginTrustMessage        : string  : Custom plugin trust warning
#   strictPluginOnlyCustomization: boolean|array: Block non-plugin customization
#   companyAnnouncements      : array   : Startup announcements
#   claudeMd                  : string  : Organization-wide CLAUDE.md content
#   claudeMdExcludes          : array   : Glob patterns to skip in CLAUDE.md loading
#   policyHelper              : string  : Executable for dynamic managed settings (v2.1.136+)
#   disableRemoteControl      : boolean : Disable Remote Control feature (v2.1.128+)
#   channelsEnabled           : boolean : Allow channels (default: true for Console API key accounts)
#   allowManagedPermissionRulesOnly: boolean: Only managed permission rules apply
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
#   ANTHROPIC_DEFAULT_HAIKU_MODEL        : Full model name for "haiku" alias (replaces ANTHROPIC_SMALL_FAST_MODEL, deprecated)
#   ANTHROPIC_DEFAULT_OPUS_MODEL_NAME/_DESCRIPTION/_SUPPORTED_CAPABILITIES: Display overrides for Opus
#   ANTHROPIC_DEFAULT_SONNET_MODEL_NAME/_DESCRIPTION/_SUPPORTED_CAPABILITIES: Display overrides for Sonnet
#   ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME/_DESCRIPTION/_SUPPORTED_CAPABILITIES: Display overrides for Haiku
#   ANTHROPIC_CUSTOM_MODEL_OPTION        : Add a single custom entry to /model picker
#   ANTHROPIC_CUSTOM_MODEL_OPTION_NAME   : Display name for custom model
#   ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION: Display description for custom model
#   CLAUDE_CODE_SUBAGENT_MODEL           : Model for all subagents/agent teams
#   CLAUDE_CODE_EFFORT_LEVEL             : Set effort level ("low","medium","high","xhigh","max")
#   CLAUDE_CODE_DISABLE_1M_CONTEXT       : 0/1 - Disable 1M context window variants
#   CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: 0/1 - Enable LLM gateway /v1/models discovery
#
# Extended Thinking:
#   CLAUDE_CODE_DISABLE_THINKING         : 0/1 - Override alwaysThinkingEnabled setting
#   CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING: 0/1 - Revert to fixed thinking budget (Opus 4.6/Sonnet 4.6)
#   MAX_THINKING_TOKENS                  : Fixed thinking budget when adaptive disabled (default: 31999, 0=disable)
#
# Auto Memory:
#   CLAUDE_CODE_DISABLE_AUTO_MEMORY      : 0/1 - Disable auto memory
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
#   CLAUDE_CODE_CERT_STORE               : CA certificate sources ("bundled", "system")
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
# Background Tasks & Agents:
#   CLAUDE_CODE_DISABLE_BACKGROUND_TASKS : 0/1 - Disable Ctrl+B, subagent backgrounds
#   CLAUDE_CODE_DISABLE_AGENT_VIEW       : 0/1 - Disable background agent view
#   CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS : 0/1 - Enable experimental agent teams
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
  claude-plugins-official,
  sharedSkillsArgs,
  ...
}:
let
  cfg = config.programs.claude-code;
  allowed = import ../allowed-commands.nix;

  # Gmail MCP Server - pinned to specific commit for security
  gmail-mcp-server = import ../../packages/gmail-mcp.nix { inherit pkgs lib; };

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
    "/nix"
  ]
  ++ cfg.extraAllowedReadDirs;

  # WebFetch domain allowlist. Domain-specific rules trigger --unshare-net,
  # which breaks Bazel (see docs/claude_code_sandbox.md). Accepted tradeoff:
  # sandboxed Bazel commands must use dangerouslyDisableSandbox: true.
  mkWebFetchDomainPerms = domains: map (domain: "WebFetch(domain:${domain})") domains;

  allowedWebFetchDomains = [
    "pypi.org"
    "docs.python.org"
    "code.claude.com"
    "files.pythonhosted.org" # PyPI wheels
    "docs.siderolabs.com" # Talos/Omni docs
    "go.dev"

    "json.schemastore.org"
    "www.schemastore.org"

    "github.com"
    "codeload.github.com"
    "raw.githubusercontent.com"
    "release-assets.githubusercontent.com"

    "app.buildbuddy.io" # BuildBuddy remote build UI
    "remote.buildbuddy.io" # BuildBuddy remote execution/cache

    "registry.npmjs.org"

    "index.crates.io"
    "static.crates.io"

    "bcr.bazel.build"
    "docs.bazel.build"
    "bazel.build"

    "docs.cilium.io"
  ]
  ++ cfg.extraAllowedWebFetchDomains;

  # Extra working directories (full read/write access, extends beyond CWD)
  # /code contains all git repos organized by host (github.com, gitlab.com, etc.)
  baseAdditionalDirs = [
    "/code"
  ]
  ++ cfg.additionalDirectories;

  # System inspection command permissions (auto-allow for read-only commands)
  inspectionPerms = import ./inspection-permissions.nix { inherit lib; };

  # Shared always-allowed command permissions.
  toBashPerm =
    entry:
    let
      suffix = if entry.type == "prefix" then ":*" else "";
    in
    "Bash(${entry.cmd}${suffix})";
  allowedCommandPerms = map toBashPerm allowed.noSudo;

  # Auto-discover all .md files in commands/ directory
  commandsDir = ./commands;
  commandFiles = builtins.readDir commandsDir;
  commands = lib.mapAttrs' (
    name: type: lib.nameValuePair (lib.removeSuffix ".md" name) (commandsDir + "/${name}")
  ) (lib.filterAttrs (name: type: type == "regular" && lib.hasSuffix ".md" name) commandFiles);

  # Shared skill files — generates home.file entries for ~/.claude/skills/
  mkSkills = import ../skills.nix sharedSkillsArgs;
  skillFiles = mkSkills { prefix = ".claude"; };

  # Parse "name@marketplace" plugin specs into { name, marketplace } attrsets
  parsedPlugins = map (
    spec:
    let
      parts = lib.splitString "@" spec;
    in
    {
      name = builtins.elemAt parts 0;
      marketplace = builtins.elemAt parts 1;
    }
  ) cfg.installPlugins;

  # Generate home.file entries for plugin cache directories
  pluginCacheFiles = lib.listToAttrs (
    map (
      p:
      lib.nameValuePair ".claude/plugins/cache/${p.marketplace}/${p.name}" {
        source = "${cfg.pluginSources.${p.marketplace}.src}/plugins/${p.name}";
        recursive = true;
      }
    ) parsedPlugins
  );

  # Generate installed_plugins.json content
  installedPluginsJson = builtins.toJSON {
    version = 2;
    plugins = lib.listToAttrs (
      map (
        p:
        let
          source = cfg.pluginSources.${p.marketplace};
        in
        lib.nameValuePair "${p.name}@${p.marketplace}" [
          {
            scope = "user";
            installPath = "${config.home.homeDirectory}/.claude/plugins/cache/${p.marketplace}/${p.name}";
            version = source.rev;
            installedAt = "1970-01-01T00:00:00.000Z";
            lastUpdated = "1970-01-01T00:00:00.000Z";
            gitCommitSha = source.rev;
          }
        ]
      ) parsedPlugins
    );
  };

  claudeOtelHeadersHelper = pkgs.writeShellApplication {
    name = "claude-otel-headers";
    runtimeInputs = [ pkgs.jq ];
    text = ''
      token_file="''${CLAUDE_CODE_OTEL_BEARER_TOKEN_FILE:-${config.sops.secrets.claude_code_otel_bearer_token.path}}"
      if [ ! -r "$token_file" ]; then
        echo "Claude Code OTEL bearer token file is not readable: $token_file" >&2
        exit 1
      fi

      token="$(cat "$token_file")"
      if [ -z "$token" ]; then
        echo "Claude Code OTEL bearer token file is empty: $token_file" >&2
        exit 1
      fi

      jq -n --arg token "$token" '{"Authorization": ("Bearer " + $token)}'
    '';
  };
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

  options.programs.claude-code.pluginSources = lib.mkOption {
    type = lib.types.attrsOf (
      lib.types.submodule {
        options = {
          src = lib.mkOption {
            type = lib.types.path;
            description = "Fetched source containing plugins/ subdirectory";
          };
          rev = lib.mkOption {
            type = lib.types.str;
            description = "Git commit SHA for version tracking in installed_plugins.json";
          };
        };
      }
    );
    default = { };
    description = "Plugin marketplace sources, keyed by marketplace name";
  };

  # Named `installPlugins`, not `plugins`, to avoid colliding with home-manager
  # 26.05's built-in `programs.claude-code.plugins` (a different, HM-native plugin
  # system). Ducktape installs these from `pluginSources` into
  # ~/.claude/plugins/cache/ and writes installed_plugins.json itself; HM's
  # `plugins`/`marketplaces` options are left unused. (wyrm2 is on 26.05, where
  # the collision breaks eval — see TODO(nixpkgs-bump) in flake.nix.)
  #
  # TODO(claude-code-plugins): properly adopt home-manager's upstream
  #   `programs.claude-code.plugins` + `.marketplaces` system and delete this
  #   custom `installPlugins` / `pluginSources` / plugin-cache / installed_plugins.json
  #   machinery. Deferred until the wholesale nixpkgs bump so all hosts share the
  #   HM-native plugin flow at once.
  options.programs.claude-code.installPlugins = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    description = "Plugins to install from pluginSources, in 'name@marketplace' format";
    example = [ "frontend-design@claude-plugins-official" ];
  };

  config.programs.claude-code = {
    enable = true;
    package = pkgsUnstable.claude-code; # Use unstable for faster updates

    inherit commands;

    pluginSources.claude-plugins-official = {
      src = claude-plugins-official;
      inherit (claude-plugins-official) rev;
    };

    installPlugins = [
      "frontend-design@claude-plugins-official"
      "pyright-lsp@claude-plugins-official"
      # Configured via repo-level rust-analyzer.toml (linkedProjects + no sccache).
      "rust-analyzer-lsp@claude-plugins-official"
    ];

    mcpServers = {
      # Disabled - Tana MCP server now in cluster
      # tana-local = {
      #   type = "http";
      #  url = "http://localhost:8262/mcp";
      # };

      # Gmail integration via MCP
      # Setup: See nix/packages/gmail-mcp.nix for full instructions
      # Quick start: gmail-mcp-auth (after configuring Google Cloud OAuth)
      gmail = {
        type = "stdio";
        command = "${gmail-mcp-server}/bin/gmail-mcp";
        args = [ ];
      };

      # SideroLabs (Talos/Omni) docs MCP server
      # Uncomment to enable — provides search over Talos and Omni documentation.
      # siderolabs = {
      #   type = "http";
      #   url = "https://docs.siderolabs.com/mcp";
      # };
    };

    settings = {
      theme = "dark";
      attribution.commit = ""; # Disable "Co-authored-by" in commits
      attribution.pr = ""; # Disable attribution in PR descriptions
      showThinkingSummaries = true;
      showTurnDuration = true; # "Cooked for Xm Ys" messages
      autoMemoryEnabled = true;
      # Voice dictation: speak prompts instead of typing. Tap once to record,
      # tap again to send. Only usable on hosts with a local microphone (the
      # laptop); a no-op on the headless agent-box/claude-web hosts. Requires a
      # claude.ai account (not API-key/Bedrock/Vertex auth).
      voice = {
        enabled = true;
        mode = "tap";
      };
      # 9999 = effectively disable cleanup (0 = delete immediately, which is wrong)
      cleanupPeriodDays = 9999;
      promptSuggestions = true;
      statusLine = {
        type = "command";
        command = "claude-statusline";
      };

      sandbox = {
        enabled = true;
        autoAllowBashIfSandboxed = true;
        allowUnsandboxedCommands = true;
        excludedCommands = [ "nvidia-smi" ];
        filesystem = {
          allowWrite = [
            "~/.cache/bazel"
            "~/.cache/bazelisk"
            "~/.cache/pre-commit"
          ];
        };
      };

      # Enable sandbox-runtime debug logging so network allow/deny decisions
      # (e.g. "Denied by config rule: telemetry.aspect.build:443") appear in
      # ~/.claude/debug/ session logs.
      env.SRT_DEBUG = "1";
      env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS = "1";
      # Native Claude Code telemetry direct to Grafana Alloy. The bearer is
      # loaded by otelHeadersHelper from the sops-nix file below so the token is
      # never embedded in settings.json.
      env.CLAUDE_CODE_ENABLE_TELEMETRY = "1";
      env.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA = "1";
      env.CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS = "1740000";
      env.OTEL_TRACES_EXPORTER = "otlp";
      env.OTEL_LOGS_EXPORTER = "otlp";
      env.OTEL_METRICS_EXPORTER = "otlp";
      env.OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf";
      env.OTEL_EXPORTER_OTLP_ENDPOINT = "https://alloy-otlp.allegedly.works";
      env.OTEL_LOG_USER_PROMPTS = "1";
      env.OTEL_LOG_TOOL_DETAILS = "1";
      env.OTEL_LOG_TOOL_CONTENT = "1";
      env.OTEL_LOG_RAW_API_BODIES = "1";
      env.CLAUDE_CODE_OTEL_BEARER_TOKEN_FILE = config.sops.secrets.claude_code_otel_bearer_token.path;
      otelHeadersHelper = "${claudeOtelHeadersHelper}/bin/claude-otel-headers";

      # Auto-generated from cfg.installPlugins
      enabledPlugins = lib.listToAttrs (
        map (spec: {
          name = spec;
          value = true;
        }) cfg.installPlugins
      );

      permissions = {
        allow = [
          "Read"
          "Edit"
          "Write"
          "MultiEdit"
          "Search"
          "Task"
          # Domain-scoped WebFetch rules auto-approve sandbox network prompts for
          # known domains. Tradeoff: triggers --unshare-net, so Bazel commands must
          # use dangerouslyDisableSandbox: true. See docs/claude_code_sandbox.md.
          "WebSearch"
        ]
        ++ allowedCommandPerms
        ++ mkWebFetchDomainPerms allowedWebFetchDomains
        ++ mkReadPerms alwaysAllowedReadDirs
        ++ inspectionPerms.permissions;
        deny = [ ];
        defaultMode = "default";
        additionalDirectories = baseAdditionalDirs;
      };
    };
  };

  config.sops.secrets.claude_code_otel_bearer_token = {
    sopsFile = ../../../secrets/alloy-otlp-bearer-token.yaml;
    key = "token";
  };

  # Add gmail-mcp-server to PATH for auth setup command
  config.home.packages = [ gmail-mcp-server ];

  # Deploy skills and plugin cache.
  config.home.file =
    skillFiles
    # Plugin cache directories
    // pluginCacheFiles;

  # TODO: If re-enabling management of installed_plugins.json, first update
  # installedPluginsJson to match Claude's current registry shape. Claude rewrites
  # installPath with a versioned suffix (for example, /pyright-lsp/1.0.0) and
  # updates mutable timestamps, which makes the old deterministic file conflict
  # on every Home Manager activation.
  # Re-enable by folding this back into config.home.file above:
  # // lib.optionalAttrs (cfg.installPlugins != [ ]) {
  #   ".claude/plugins/installed_plugins.json".text = installedPluginsJson;
  # };
}
