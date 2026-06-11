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
  execPolicyRules = import ./execpolicy-rules.nix { inherit lib; };
  codexNpmCache = "${config.xdg.cacheHome}/codex/npm";
  codexNixCache = "${config.xdg.cacheHome}/nix";
  codexBazelCache = "${config.xdg.cacheHome}/bazel";
  codexBazeliskCache = "${config.xdg.cacheHome}/bazelisk";
  codexPreCommitCache = "${config.xdg.cacheHome}/pre-commit";
  codexSccacheCache = "${config.xdg.cacheHome}/sccache";

  codexSettings = {
    model = "gpt-5.5";
    model_reasoning_effort = "xhigh";
    plan_mode_reasoning_effort = "xhigh";

    # Local model providers for GPT-OSS
    model_providers = {
      # vllm provider disabled: wire_api = "chat" is no longer supported (2026-04-21).
      # vLLM's Responses API has incorrect GPT-OSS handling:
      # https://github.com/vllm-project/vllm/issues/28262
      # Re-enable with wire_api = "responses" once that issue is fixed.
      # vllm = {
      #   name = "vLLM Local";
      #   base_url = "http://localhost:8000/v1";
      #   wire_api = "responses";
      # };
      cluster = {
        name = "Cluster (litellm.allegedly.works)";
        base_url = "https://litellm.allegedly.works/v1";
        env_key = "OLLAMA_API_KEY";
        wire_api = "responses";
      };
    };

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
    # Multiple profiles let you switch between open‑source and OpenAI models.
    # The UI can pick a profile via the `/select_profile` command or by setting
    # `--config profile=NAME` when launching Codex.
    profiles = {
      openai = {
        model = "gpt-5.1-codex";
        # Web search requires OpenAI backend.
        web_search = "live";
      };
      # GPT-OSS-20B via vLLM with Responses API
      gpt-oss = {
        model = "gpt-oss-20b";
        model_provider = "vllm";
        model_reasoning_effort = "high";
        web_search = "disabled";
      };
      # GPT-OSS-20B via Ollama
      gpt-oss-ollama = {
        model = "gpt-oss:20b";
        model_provider = "ollama";
        web_search = "disabled";
      };
      # GPT-OSS 20B via cluster LiteLLM (228 t/s decode, 100% GPU)
      # Run: codex --config profile=gpt-oss-20b
      gpt-oss-20b = {
        model = "gpt-oss-20b-128k";
        model_provider = "cluster";
        model_reasoning_effort = "high";
        web_search = "disabled";
      };
      # GPT-OSS 120B via cluster LiteLLM (10 t/s decode, 91% GPU)
      # Run: codex --config profile=gpt-oss-120b
      gpt-oss-120b = {
        model = "gpt-oss-120b-128k";
        model_provider = "cluster";
        model_reasoning_effort = "high";
        web_search = "disabled";
      };
    };
    # Persist command history to disk.
    # "save-all" will write every turn to ~/.codex/history.jsonl
    history = {
      persistence = "save-all";
    };
    # Pair the built-in trusted-command heuristic with our generated execpolicy
    # allow rules under $CODEX_HOME/rules/default.rules so safe commands auto-run,
    # while the agent can explicitly request approval to run other commands
    # outside the sandbox when needed.
    #
    # Note: a surrounding web/host harness can still override this at runtime.
    # This setting controls locally launched Codex sessions that read
    # ~/.codex/config.toml.
    approval_policy = "on-request";
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
    sandbox_mode = "workspace-write";
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

  mergeScript = ''
    set -euo pipefail

    CODEX_HOME='${codexHomeAbsolute}'
    BASE='${baseFileAbsolute}'
    LIVE='${liveFileAbsolute}'

    if [ ! -f "$BASE" ]; then
      exit 0
    fi

    mkdir -p "$CODEX_HOME"
    mkdir -p '${codexNpmCache}'
    mkdir -p '${codexNixCache}'
    mkdir -p '${codexPreCommitCache}'
    mkdir -p '${codexSccacheCache}'
    mkdir -p '${codexBazelCache}'
    mkdir -p '${codexBazeliskCache}'

    BASE="$BASE" LIVE="$LIVE" ${pythonMerge}/bin/python ${./merge.py}
  '';
in
{
  programs.codex = {
    enable = true;
    # Prefer the unstable codex package if available.
    package = pkgsUnstable.codex;
    # Avoid letting the upstream module overwrite ~/.codex/config.toml.
    # The activation script below handles merging our desired settings.
  };

  home = {
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
    activation.codexConfig = lib.hm.dag.entryAfter [ "writeBoundary" ] mergeScript;
    sessionVariables = lib.mkIf useXdgDirectories {
      CODEX_HOME = "${config.xdg.configHome}/codex";
    };
  };
}
