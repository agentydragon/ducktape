# Codex configuration module
{
  pkgs,
  pkgsMaster,
  lib,
  config,
  sharedSkillsArgs,
  ...
}:
let
  cfg = config.ducktape.codex;
  execPolicyRules = import ./execpolicy-rules.nix { inherit lib; };
  codexNpmCache = "${config.xdg.cacheHome}/codex/npm";
  codexNixCache = "${config.xdg.cacheHome}/nix";
  codexBazelCache = "${config.xdg.cacheHome}/bazel";
  codexBazeliskCache = "${config.xdg.cacheHome}/bazelisk";
  codexPreCommitCache = "${config.xdg.cacheHome}/pre-commit";
  codexSccacheCache = "${config.xdg.cacheHome}/sccache";
  # Current Codex host-owned GitHub app connector id. Codex matches app approval
  # config by connector id from the tool's MCP metadata, not by display name.
  githubCodexAppsConnectorId = "connector_76869538009648d5b282a4bb21c3d157";

  # Common base config for every host. Cluster/local (gpt-oss) model providers +
  # profiles live in localModelSettings (opt-in via ducktape.codex.localModels);
  # the writable-roots sandbox block is appended only under workspace-write.
  baseSettings = {
    model = "gpt-5.6-sol";
    # To exceed a model's default, set `model_context_window = <tokens>;` here;
    # Codex clamps it to that model's catalogued maximum (Astra: 872000).
    model_reasoning_effort = "medium";
    plan_mode_reasoning_effort = "medium";

    features = {
      streamable_shell = true;
      rmcp_client = true;
      unified_exec = true;
      view_image_tool = true;
      shell_tool = true; # enable `/shell`
      apply_patch_freeform = true; # freeform patch syntax
      memories = true; # experimental memory read/write pipeline
      # shell_snapshot ?  (keep off unless you want offline TUI snapshots)
    };
    # Persist command history to disk.
    # "save-all" will write every turn to ~/.codex/history.jsonl
    history = {
      persistence = "save-all";
    };
    mcp_servers = {
      "haku-console" = {
        url = "https://haku.allegedly.works/mcp";
        auth = "oauth";
        default_tools_approval_mode = "approve";
      };
    };
    apps = {
      ${githubCodexAppsConnectorId} = {
        tools = {
          # Raw tool names from `codex app-server`'s host-owned `codex_apps`
          # server for the GitHub connector. Codex also matches tool titles, so
          # include those to tolerate tool namespace churn under the same app id.
          github_create_pull_request = {
            approval_mode = "approve";
          };
          github_update_pull_request = {
            approval_mode = "approve";
          };
          create_pull_request = {
            approval_mode = "approve";
          };
          update_pull_request = {
            approval_mode = "approve";
          };
        };
      };
    };
    # Pair the built-in trusted-command heuristic with our generated execpolicy
    # allow rules under $CODEX_HOME/rules/managed.rules so safe commands auto-run,
    # while the agent can explicitly request approval to run other commands
    # outside the sandbox when needed. Codex owns default.rules for amendments
    # accepted through the approval UI.
    #
    # Note: a surrounding web/host harness can still override this at runtime.
    # This setting controls locally launched Codex sessions that read
    # ~/.codex/config.toml.
    approval_policy = cfg.approvalPolicy;
    # Let Codex's separate reviewer agent handle approval requests that cross
    # the sandbox boundary; this preserves the interactive approval policy and
    # does not expand the sandbox or writable roots.
    approvals_reviewer = cfg.approvalsReviewer;
    # TODO: Consider a personal [auto_review].policy for command, data-access, and destructive-action limits.
    shell_environment_policy = {
      "inherit" = "all";
      "set" = {
        CODEX_AGENT = "1";
        # Keep npm installs from node-based pre-commit hooks inside a
        # Codex-owned cache that is writable from the sandbox.
        NPM_CONFIG_CACHE = codexNpmCache;
        BAZELISK_HOME = codexBazeliskCache;
      };
    };
    sandbox_mode = cfg.sandboxMode;
  }
  # writable_roots only apply under workspace-write; danger-full-access (an
  # isolated agent VM) drops the whole block — everything the user can write is
  # writable, with no list.
  // lib.optionalAttrs (cfg.sandboxMode == "workspace-write") {
    sandbox_workspace_write = {
      writable_roots = [
        codexSccacheCache
        codexNixCache
        # Do not add /nix here. Codex/bubblewrap prepares synthetic mount
        # blockers below writable roots, including .git sentinels such as
        # /nix/.git, to keep repository metadata from leaking into the
        # sandbox. /nix is root-owned, and /nix/store is immutable, so marking
        # it writable makes sandbox startup fail before the requested command
        # runs. Use the user-owned ~/.cache/nix cache for sandboxed Nix state;
        # run real Nix store writes outside the sandbox.
        codexPreCommitCache
        # Writable roots must be directories. The pre-commit hook log is a file;
        # listing it directly makes sandbox startup inspect
        # ~/.cache/pre-commit/pre-commit.log/.codex and fail with ENOTDIR. The
        # parent directory above is enough for pre-commit to update the log.
        codexNpmCache
        codexBazelCache
        codexBazeliskCache
      ];
      network_access = true;
      exclude_tmpdir_env_var = false;
      exclude_slash_tmp = false;
    };
  };

  # Cluster/local (gpt-oss) model providers + profiles. Workstation-only; agent
  # VMs running OpenAI Codex don't need them. Opt in via ducktape.codex.localModels.
  localModelSettings = {
    model_providers.cluster = {
      name = "Cluster (litellm.allegedly.works)";
      base_url = "https://litellm.allegedly.works/v1";
      env_key = "OLLAMA_API_KEY";
      wire_api = "responses";
    };
    profiles = {
      # GPT-OSS-20B via Ollama
      gpt-oss-ollama = {
        model = "gpt-oss:20b";
        model_provider = "ollama";
        web_search = "disabled";
      };
      # GPT-OSS 20B via cluster LiteLLM
      gpt-oss-20b = {
        model = "gpt-oss-20b-128k";
        model_provider = "cluster";
        model_reasoning_effort = "high";
        web_search = "disabled";
      };
      # GPT-OSS 120B via cluster LiteLLM
      gpt-oss-120b = {
        model = "gpt-oss-120b-128k";
        model_provider = "cluster";
        model_reasoning_effort = "high";
        web_search = "disabled";
      };
    };
  };

  codexSettings = lib.recursiveUpdate baseSettings (
    lib.optionalAttrs cfg.localModels.enable localModelSettings
  );

  tomlFormat = pkgs.formats.toml { };
  baseConfigFile = tomlFormat.generate "codex-config.nix-base" codexSettings;

  useXdgDirectories = config.home.preferXdgDirectories;
  xdgConfigHomeRelative = lib.removePrefix "${config.home.homeDirectory}/" config.xdg.configHome;
  codexHomeRelative = if useXdgDirectories then "${xdgConfigHomeRelative}/codex" else ".codex";
  codexHomeAbsolute =
    if useXdgDirectories then
      "${config.xdg.configHome}/codex"
    else
      "${config.home.homeDirectory}/.codex";

  baseFileRelative = "${codexHomeRelative}/config.nix-base.toml";
  baseFileAbsolute = "${codexHomeAbsolute}/config.nix-base.toml";
  liveFileAbsolute = "${codexHomeAbsolute}/config.toml";
  rulesDirectoryAbsolute = "${codexHomeAbsolute}/rules";
  managedRulesFileAbsolute = "${rulesDirectoryAbsolute}/managed.rules";
  defaultRulesFileAbsolute = "${rulesDirectoryAbsolute}/default.rules";
  rulesReadmeRelative = "${codexHomeRelative}/rules/README.md";
  skillPrefix = if useXdgDirectories then "${xdgConfigHomeRelative}/codex" else ".codex";

  pythonMerge = pkgs.python3.withPackages (ps: [ ps."tomli-w" ]);
  managedRulesSource = pkgs.writeText "codex-managed.rules" execPolicyRules.text;
  mkSkills = import ../skills.nix sharedSkillsArgs;
  # Codex currently skips symlinked SKILL.md files, so deploy each skill as a directory symlink.
  skillFiles = mkSkills {
    prefix = skillPrefix;
    mode = "directory-symlink";
  };

  # No `exit`/`set -e` here: home-manager concatenates every activation snippet
  # into one script, so either would derail the WHOLE activation — skipping
  # linkGeneration/reloadSystemd and leaving all home files + user systemd units
  # unlinked. (That bit a first-ever activation on a freshly-imaged headless
  # host, where this snippet runs before linkGeneration has placed $BASE; masked
  # on long-lived hosts where $BASE already exists.) merge.py already no-ops when
  # the base config isn't present, so no shell guard is needed.
  mergeScript = ''
    for dir in \
      '${codexHomeAbsolute}' \
      '${codexNpmCache}' \
      '${codexNixCache}' \
      '${codexPreCommitCache}' \
      '${codexSccacheCache}' \
      '${codexBazelCache}' \
      '${codexBazeliskCache}'; do
      mkdir -p "$dir"
    done

    BASE='${baseFileAbsolute}' LIVE='${liveFileAbsolute}' ${pythonMerge}/bin/python ${./merge.py}
  '';

  materializeExecPolicyRules = ''
    rules_dir='${rulesDirectoryAbsolute}'
    managed_rules='${managedRulesFileAbsolute}'
    default_rules='${defaultRulesFileAbsolute}'

    mkdir -p "$rules_dir"

    # Home Manager used to own default.rules as a symlink. Codex ignores
    # symlinked *.rules during automatic discovery and reserves default.rules
    # for amendments accepted through its approval UI. linkGeneration normally
    # removes the obsolete managed link before this activation step; this check
    # also handles an interrupted/partial migration. Preserve any regular file
    # containing Codex-owned amendments.
    if [ -L "$default_rules" ]; then
      default_rules_target=$(readlink "$default_rules")
      case "$default_rules_target" in
        /nix/store/*-home-manager-files/.codex/rules/default.rules)
          rm "$default_rules"
          ;;
        *)
          echo "Refusing to replace non-Home-Manager default.rules symlink: $default_rules -> $default_rules_target" >&2
          false
          ;;
      esac
    fi
    if [ ! -e "$default_rules" ]; then
      touch "$default_rules"
      chmod 0600 "$default_rules"
    fi

    # Codex 0.144.1 only discovers directory entries whose file type is a
    # regular file. Materialize the declarative rules instead of exposing the
    # usual Home Manager symlink into the Nix store.
    managed_rules_tmp="$rules_dir/.managed.rules.tmp"
    install -m 0600 '${managedRulesSource}' "$managed_rules_tmp"
    mv -f "$managed_rules_tmp" "$managed_rules"

    # Automatic discovery requires a real directory entry. Rule semantics are
    # exercised separately by the codex-execpolicy-evaluation flake check so
    # Home Manager activation does not need to launch Codex on every switch.
    if [ ! -f "$managed_rules" ] || [ -L "$managed_rules" ]; then
      echo "Codex managed exec policy is not a regular file: $managed_rules" >&2
      false
    fi
  '';
in
{
  # Per-host knobs. Values pass straight through to Codex's config.toml keys;
  # workstations keep the defaults, an isolated agent VM goes full-auto.
  options.ducktape.codex = {
    approvalPolicy = lib.mkOption {
      type = lib.types.str;
      default = "on-request";
      description = "Codex `approval_policy` (e.g. \"on-request\", \"never\").";
    };
    approvalsReviewer = lib.mkOption {
      type = lib.types.str;
      default = "auto_review";
      description = "Codex `approvals_reviewer` (e.g. \"user\", \"auto_review\").";
    };
    sandboxMode = lib.mkOption {
      type = lib.types.str;
      default = "workspace-write";
      description = ''
        Codex `sandbox_mode` (e.g. "workspace-write", "danger-full-access").
        Only "workspace-write" emits the writable-roots block.
      '';
    };
    localModels.enable = lib.mkEnableOption "the cluster/local (gpt-oss) Codex model providers + profiles";
  };

  config.programs.codex = {
    enable = true;
    # Codex ships frequently; use the narrow master input instead of moving whole hosts.
    package = pkgsMaster.codex;
    # Avoid letting the upstream module overwrite ~/.codex/config.toml.
    # The activation script below handles merging our desired settings.
  };

  config.home = {
    file = {
      "${baseFileRelative}".source = baseConfigFile;
      "${rulesReadmeRelative}".text = ''
        Codex execpolicy rules
        =====================

        This directory contains both declarative and Codex-owned policy state.

        - `managed.rules` is generated from `nix/home/allowed-commands.nix` and
          materialized as a regular file because Codex ignores symlinked rules.
        - `default.rules` is writable state owned by Codex for amendments accepted
          through the approval UI. Home Manager preserves it across switches.
        - Codex loads every `*.rules` file in this directory automatically.
        - The generated rules are prefix-based `allow` entries.

        Syntax pointers:

        - `prefix_rule(pattern=["git", "status"], decision="allow")`
        - `prefix_rule(pattern=["git", "commit"], decision="prompt", justification="history-changing")`
        - `prefix_rule(pattern=["rm"], decision="forbidden", justification="destructive")`

        Local checks:

        - `codex execpolicy check --pretty --rules "$CODEX_HOME/rules/managed.rules" -- git status`
        - `codex execpolicy check --pretty --rules "$CODEX_HOME/rules/managed.rules" -- bash -lc 'git status'`

        Notes:

        - Matching `decision="allow"` rules bypass Codex's shell sandbox for
          the matched command prefix.
        - `match=` / `not_match=` are validation examples at rule-load time.
        - They are not exact-match enforcement, so this generator refuses
          `type = "exact"` entries from the shared SSOT.
      '';
    }
    // skillFiles;
    activation = {
      codexConfig = lib.hm.dag.entryAfter [ "writeBoundary" ] mergeScript;
      codexExecPolicyRules = lib.hm.dag.entryAfter [ "linkGeneration" ] materializeExecPolicyRules;
    };
    sessionVariables = lib.mkIf useXdgDirectories {
      CODEX_HOME = "${config.xdg.configHome}/codex";
    };
  };
}
