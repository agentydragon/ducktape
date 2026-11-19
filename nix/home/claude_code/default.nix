# Claude Code Configuration Module
#
# Settings and environment variables are documented upstream:
#   - https://code.claude.com/docs/en/settings.md
#   - https://code.claude.com/docs/en/network-config.md
#   - https://code.claude.com/docs/en/model-config.md
# JSON Schema: https://json.schemastore.org/claude-code-settings.json
#
# Unverified behavior this module depends on:
#   - permissions.defaultMode "auto" is ignored in project/local settings. An older note here said it
#     was managed-settings-only as of v2.1.142; /doctor guidance shipped with 2.1.220 says user scope
#     grants it. Check `/status` after a switch: if it reads "default", the stricter reading was right
#     and this setting is inert.
#   - The built-in "$defaults" content of autoMode arrays is not publicly documented. The
#     `claude auto-mode` subcommand exists in help, but its sub-subcommands (defaults/config/critique)
#     may not work in all versions.
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
  mcpServers = import ../mcp-servers.nix;

  # Helper to generate recursive file-read permissions for directories.
  # Read(path) rules cover every file-reading tool; Grep(path) and Glob(path)
  # do not participate in Claude Code's file permission checks.
  # Pattern syntax: https://code.claude.com/docs/en/settings
  #   - Supports glob patterns: ** for recursive, * for wildcard
  #   - Supports ~ for home directory expansion
  mkReadPerms = dirs: map (dir: "Read(${dir}/**)") dirs;

  # Directories where file-reading tools are always allowed without prompting.
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
    description = "Additional directories to auto-allow for file-reading operations";
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

    mcpServers = lib.mapAttrs (name: server: {
      type = "http";
      inherit (server) url;
    }) mcpServers;

    settings = {
      theme = "dark";
      agentPushNotifEnabled = true;
      attribution.commit = ""; # Disable "Co-authored-by" in commits
      attribution.pr = ""; # Disable attribution in PR descriptions
      showThinkingSummaries = true;
      showTurnDuration = true; # "Cooked for Xm Ys" messages
      autoMemoryEnabled = true;
      autoMode = {
        environment = [
          "### Org-wide"
          ''**Organization**: None configured — personal infrastructure monorepo (per README: "Personal infrastructure monorepo"), single-author (agentydragon)''
          "**Cloud provider(s)**: None configured — evidence shows self-hosted k8s (Proxmox/Talos, host 'atlas') and Forgejo, not a named commercial cloud provider account"
          "**Repository visibility**: PUBLIC — github.com/agentydragon/ducktape (confirmed via gh); any push here is publishing"
          "**Internal sharing / snippet hosting**: None configured — treat public paste/gist services as outside the trust boundary"
          "**Secrets management**: SOPS with age keys (SOPS_AGE_KEY, SOPS_VERSION markers; sops.yaml patterns in cluster/); k8s Secrets referenced (e.g. props-evaluator-credentials, ollama-api-key)"
          "**Default / protected branches**: devel (default, protected via ruleset 'default-branch-protection', active) — treat devel on github.com/agentydragon/ducktape as protected"
          "**CI/CD deploy targets**: GitHub Actions (.github/workflows) and Forgejo (git.allegedly.works/agentydragon/ducktape.git) — Flux/flux-system namespace for GitOps deploys to the atlas k8s cluster"
          "**Network posture**: gVisor-sandboxed Claude Code web containers used for some work; DOCKER_HOST/podman used for local props testing"
          "**Source control**: this repo (agentydragon/ducktape) and its two remotes — origin (github.com:agentydragon/ducktape) and forgejo (git.allegedly.works:agentydragon/ducktape.git)"
          "**Trusted cloud buckets**: None configured"
          "**Key internal services**: props backend at https://props.allegedly.works (evaluator API); local Postgres at 127.0.0.1:5433 (eval_results db) for props dev/testing; atlas k8s cluster (Proxmox/Talos) reached via kubectl across namespaces (haku-console, forgejo, flux-system, haku-sandbox, kube-system, activitywatch, grocy-sf, seaweedfs, loki, monitoring, nix-cache, agents-infra, authentik)"
          "**Internal package registry**: local OCI registry proxy at 127.0.0.1:8000 (props testing); ghcr.io/agentydragon/rbe-worker for BuildBuddy RBE image"
          "**Sensitive data locations & audiences**: cluster/ IaC and secrets docs (cluster/docs/secrets.md, cluster/k8s/*/terraform.yaml, *.sops.yaml files, aws-credentials.sops.yaml, s3-credentials.yaml), devinfra/claude/claude_api/credentials.py, haku/console kubernetes_grant_* files, finance/plaid/db/secret_store.py, airlock/oauth/k8s_client.py — share only with the repo owner (agentydragon)"
          "**Data retention / declassification**: None configured"
          "**Sensitive remote targets**: any namespace/host with 'prod' or 'production' as a whole word/segment (none observed by name in this recon); treat the live props deployment at props.allegedly.works and the atlas k8s cluster's production-facing namespaces (e.g. haku-console) with the same caution as prod given they are live-shared infra"
          "**Protected deployment namespaces / environments**: None configured — fall back to the Sensitive remote targets heuristic; note high-traffic shared namespaces observed: haku-console, forgejo, flux-system, kube-system"
          "**Protected IaC scopes**: IAM, RBAC, networking, quota, node-pool resources; anything tagged prod/production as a whole word/segment — cluster/terraform/main/*.tf (cilium, flux, home-nodes, infrastructure, nebula, ovh-nodes, proxmox-nodes/vms, talos-ccm, talos-machine-secrets), cluster/cdk8s/descheduler.py, cluster/docs/iam-policy-route53.json"
          "### User-specific"
          "**Primary use of Claude Code**: software development — personal infrastructure monorepo (Bazel/Nix/k8s/Rust/Python) plus props ML-eval-pipeline development, agent trace debugging, and Claude Code container reverse-engineering"
          "**Trusted repo**: this repo, agentydragon/ducktape, working dir /home/agentydragon/code/ducktape, and its two remotes (github.com/agentydragon/ducktape [public — only this repo's own work should be pushed there] and git.allegedly.works/agentydragon/ducktape.git); confidential/secret material must never be committed to either regardless of visibility"
          "**Org-specific CLIs**: bb / bbr / bbapi (Bazel/BuildBuddy wrappers), direnv, kubectl (atlas cluster), talosctl, sops, nixos-rebuild, z-claude, aiquota, codex, ghreq"
          "routine under /home/agentydragon/code/ducktape/ prefix: Bazel builds/tests, gazelle BUILD regen, props critic/grader dev workflow, kubectl reads against the atlas cluster namespaces listed above, Forgejo/GitHub read operations"
        ];
        allow = [
          "$defaults"
          "Bash(bb run //devinfra:gazelle:*) in agentydragon/ducktape — routine BUILD file regeneration"
          "Bash(bazel test //props/...) in agentydragon/ducktape — routine test workflow distinct from manual live-deployment skill"
          "Bash(kubectl get:*) and Bash(kubectl logs:*) read-only ops against atlas cluster namespaces seen routinely (haku-console, forgejo, flux-system, kube-system, monitoring, loki)"
        ];
        soft_deny = [
          "$defaults"
          "kubectl apply/delete/exec against atlas cluster namespaces outside a scratch/dev namespace (e.g. haku-console, flux-system, kube-system, authentik) — shared production-adjacent infra"
          "Writes to cluster/terraform/main/*.tf or cluster/k8s/*/terraform.yaml (IaC affecting IAM, networking, node pools, secrets sync) without explicit confirmation"
        ];
      };
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
      # Claude Code defaults to delta temporality, which Prometheus/Mimir cannot
      # ingest: Alloy's otelcol.exporter.prometheus drops delta metrics silently
      # (no error, no counter), so metrics vanish between the OTLP receiver and
      # remote_write while traces and logs — which have no temporality — arrive
      # fine. Converting server-side would need otelcol.processor.deltatocumulative,
      # which is experimental and would force the whole Alloy instance to
      # --stability.level=experimental. Setting it here is the supported knob.
      env.OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE = "cumulative";
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
        # Auto-approve every tool of every SSOT-configured MCP server.
        ++ map (name: "mcp__${name}__*") (builtins.attrNames mcpServers)
        ++ allowedCommandPerms
        ++ mkWebFetchDomainPerms allowedWebFetchDomains
        ++ mkReadPerms alwaysAllowedReadDirs
        ++ inspectionPerms.permissions;
        deny = [ ];
        # "auto" delegates per-action approval to the safety classifier instead of
        # prompting. Only policy/user-scope settings may grant it — a project or
        # local settings.json setting "auto" is ignored as repo-controllable.
        defaultMode = "auto";
        additionalDirectories = baseAdditionalDirs;
      };
    };
  };

  config.sops.secrets.claude_code_otel_bearer_token = {
    sopsFile = ../../../secrets/alloy-otlp-bearer-token.yaml;
    key = "token";
  };

  # Deploy skills and plugin cache.
  config.home.file =
    skillFiles
    # Plugin cache directories
    // pluginCacheFiles;

  # Claude owns installed_plugins.json: its versioned paths and mutable timestamps
  # conflict with a deterministic Home Manager file on activation.
}
