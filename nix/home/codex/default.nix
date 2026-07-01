# Codex configuration module
{
  pkgs,
  pkgsUnstable,
  lib,
  config,
  sharedSkillsArgs,
  ...
}:
let
  cfg = config.ducktape.codex;
  inherit (config.ducktape) cachePaths;
  execPolicyRules = import ./execpolicy-rules.nix { inherit lib; };
  # Current Codex host-owned GitHub app connector id. Codex matches app approval
  # config by connector id from the tool's MCP metadata, not by display name.
  githubCodexAppsConnectorId = "connector_76869538009648d5b282a4bb21c3d157";

  # Common base config for every host. Cluster/local (gpt-oss) model providers +
  # profiles live in localModelSettings (opt-in via ducktape.codex.localModels);
  # the writable-roots sandbox block is appended only under workspace-write.
  baseSettings = {
    model = "gpt-5.5";
    model_reasoning_effort = "xhigh";
    plan_mode_reasoning_effort = "xhigh";

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
    # allow rules under $CODEX_HOME/rules/default.rules so safe commands auto-run,
    # while the agent can explicitly request approval to run other commands
    # outside the sandbox when needed.
    #
    # Note: a surrounding web/host harness can still override this at runtime.
    # This setting controls locally launched Codex sessions that read
    # ~/.codex/config.toml.
    approval_policy = cfg.approvalPolicy;
    shell_environment_policy = {
      "inherit" = "all";
      "set" = {
        CODEX_AGENT = "1";
        # Keep npm installs from node-based pre-commit hooks inside a
        # Codex-owned cache that is writable from the sandbox.
        NPM_CONFIG_CACHE = cachePaths.codexNpm;
        BAZELISK_HOME = cachePaths.bazelisk;
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
        cachePaths.nix
        # Do not add /nix here. Codex/bubblewrap prepares synthetic mount
        # blockers below writable roots, including .git sentinels such as
        # /nix/.git, to keep repository metadata from leaking into the
        # sandbox. /nix is root-owned, and /nix/store is immutable, so marking
        # it writable makes sandbox startup fail before the requested command
        # runs. Use the user-owned ~/.cache/nix cache for sandboxed Nix state;
        # run real Nix store writes outside the sandbox.
        cachePaths.preCommit
        # Writable roots must be directories. The pre-commit hook log is a file;
        # listing it directly makes sandbox startup inspect
        # ~/.cache/pre-commit/pre-commit.log/.codex and fail with ENOTDIR. The
        # parent directory above is enough for pre-commit to update the log.
        cachePaths.codexNpm
        cachePaths.bazel
        cachePaths.bazelisk
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
  rulesFileRelative = "${codexHomeRelative}/rules/default.rules";
  rulesReadmeRelative = "${codexHomeRelative}/rules/README.md";
  skillPrefix = if useXdgDirectories then "${xdgConfigHomeRelative}/codex" else ".codex";

  pythonMerge = pkgs.python3.withPackages (ps: [ ps."tomli-w" ]);
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
    BASE='${baseFileAbsolute}' LIVE='${liveFileAbsolute}' ${pythonMerge}/bin/python ${./merge.py}
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

  config.ducktape.cacheDirs = [
    codexHomeAbsolute
    cachePaths.codexNpm
    cachePaths.nix
    cachePaths.bazel
    cachePaths.bazelisk
    cachePaths.preCommit
  ];

  config.programs.codex = {
    enable = true;
    # Prefer the unstable codex package if available.
    package = pkgsUnstable.codex;
    # Avoid letting the upstream module overwrite ~/.codex/config.toml.
    # The activation script below handles merging our desired settings.
  };

  config.home = {
    file = {
      "${baseFileRelative}".source = baseConfigFile;
      "${rulesFileRelative}".text = execPolicyRules.text;
      "${rulesReadmeRelative}".text = ''
        Codex execpolicy rules
        =====================

        This directory is managed by Home Manager.

        - `default.rules` is generated from `nix/home/allowed-commands.nix`.
        - Codex loads every `*.rules` file in this directory automatically.
        - The generated rules are prefix-based `allow` entries.

        Syntax pointers:

        - `prefix_rule(pattern=["git", "status"], decision="allow")`
        - `prefix_rule(pattern=["git", "commit"], decision="prompt", justification="history-changing")`
        - `prefix_rule(pattern=["rm"], decision="forbidden", justification="destructive")`

        Local checks:

        - `codex-execpolicy check --pretty --rules "$CODEX_HOME/rules/default.rules" -- git status`
        - `codex-execpolicy check --pretty --rules "$CODEX_HOME/rules/default.rules" -- bash -lc 'git status'`

        Notes:

        - Matching `decision="allow"` rules bypass Codex's shell sandbox for
          the matched command prefix.
        - `match=` / `not_match=` are validation examples at rule-load time.
        - They are not exact-match enforcement, so this generator refuses
          `type = "exact"` entries from the shared SSOT.
      '';
    }
    // skillFiles;
    activation.codexConfig = lib.hm.dag.entryAfter [ "ducktapeCacheDirs" ] mergeScript;
    sessionVariables = lib.mkIf useXdgDirectories {
      CODEX_HOME = "${config.xdg.configHome}/codex";
    };
  };
}
