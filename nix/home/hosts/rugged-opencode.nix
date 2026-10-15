# OpenCode local-LLM provider wiring for rugged's on-device inference.
#
# TODO: activate the authenticated in-cluster serving route from PR #10452
# after its deployment is validated.
{
  pkgs,
  pkgsUnstable,
  osConfig ? null,
  ...
}:
let
  offlineProvider = "rugged-offline";
  routerEnabled =
    if osConfig == null then false else osConfig.ducktape.localLlm.llamaRouter.enable or false;
  backendLabel = if routerEnabled then "llama.cpp router" else "Ollama";
  offlineModels = {
    qwen3_4b = {
      id = "qwen3:4b-instruct-2507-q4_K_M";
      name = "Qwen3 4B | 32k | tools OK";
      context = 32768;
      output = 4096;
      toolCall = true;
    };
    gemma4_e2b = {
      id = "gemma4:e2b-it-qat";
      name = "Gemma4 E2B QAT | 32k | tools OK (Ollama)";
      context = 32768;
      output = 8192;
      toolCall = true;
    };
    gemma4_e2b_long_128k_experimental = {
      id = "gemma4:e2b-it-qat-long-128k-b128";
      name = "Gemma4 E2B QAT | 128k EXP | recall 1/3";
      context = 131072;
      output = 4096;
      toolCall = true;
    };
    qwen35_0_8b_long_256k_experimental = {
      id = "qwen3.5:0.8b-long-256k-b256";
      name = "Qwen3.5 0.8B Q8 | 256k EXP | tools OK";
      context = 262144;
      output = 4096;
      toolCall = true;
    };
    gemma4_e4b = {
      id = "gemma4:e4b";
      name = "Gemma4 E4B | 32k | tools OK";
      context = 32768;
      output = 4096;
      toolCall = true;
    };
    qwen2_5_coder_7b = {
      id = "qwen2.5-coder:7b";
      name = "Qwen2.5 Coder 7B | 32k | tools untested";
      context = 32768;
      output = 4096;
      toolCall = false;
    };
    qwen3_8b = {
      id = "qwen3:8b";
      name = "Qwen3 8B | 32k | tools untested";
      context = 32768;
      output = 4096;
      toolCall = false;
    };
  };
  offlineConfigFor =
    key: model:
    let
      modelsForPicker =
        if routerEnabled then
          offlineModels
        else
          builtins.removeAttrs offlineModels [ "qwen35_0_8b_long_256k_experimental" ];
    in
    builtins.toFile "rugged-opencode-offline-${key}.json" (
      builtins.toJSON {
        "$schema" = "https://opencode.ai/config.json";
        model = "${offlineProvider}/${model.id}";
        default_agent = "local-build";
        subagent_depth = 0;
        plugin = [ ];
        mcp = { };
        agent.local-build = {
          reasoningEffort = "none";
          temperature = 0.2;
          top_p = 0.95;
          steps = 20;
          permission = {
            "*" = "deny";
            read = "allow";
            edit = "allow";
            write = "allow";
            bash = "allow";
            glob = "allow";
            grep = "allow";
            list = "allow";
          };
        };
        provider.${offlineProvider} = {
          npm = "@ai-sdk/openai-compatible";
          name = if routerEnabled then "Rugged local llama.cpp router" else "Rugged local Ollama";
          options = {
            baseURL = "http://127.0.0.1:11436/v1";
            timeout = false;
            headerTimeout = false;
            chunkTimeout = false;
          };
          models = builtins.listToAttrs (
            map (rosterModel: {
              name = rosterModel.id;
              value = {
                inherit (rosterModel) name;
                reasoning = false;
                temperature = true;
                tool_call = rosterModel.toolCall;
                limit = {
                  inherit (rosterModel) context output;
                };
              };
            }) (builtins.attrValues modelsForPicker)
          );
        };
      }
    );
  offlineConfigs = builtins.mapAttrs offlineConfigFor offlineModels;
  qwen35SelectorCase =
    if routerEnabled then
      ''
        qwen3.5-0.8b-long-256k-experimental)
          selected_model="${offlineModels.qwen35_0_8b_long_256k_experimental.id}"
          selected_config="${offlineConfigs.qwen35_0_8b_long_256k_experimental}"
          ;;
      ''
    else
      ''
        qwen3.5-0.8b-long-256k-experimental)
          echo "Qwen3.5 0.8B requires the rugged llama.cpp router backend to be enabled" >&2
          exit 2
          ;;
      '';

  offlineCommand = pkgs.writeShellApplication {
    name = "rugged-opencode-offline-inner";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gnused
      pkgsUnstable.opencode
    ];
    text = ''
      case "''${RUGGED_OPENCODE_MODEL:-qwen3-4b}" in
        qwen3-4b)
          selected_model="${offlineModels.qwen3_4b.id}"
          selected_config="${offlineConfigs.qwen3_4b}"
          ;;
        gemma4-e2b)
          selected_model="${offlineModels.gemma4_e2b.id}"
          selected_config="${offlineConfigs.gemma4_e2b}"
          ;;
        gemma4-e2b-long-128k-experimental)
          selected_model="${offlineModels.gemma4_e2b_long_128k_experimental.id}"
          selected_config="${offlineConfigs.gemma4_e2b_long_128k_experimental}"
          ;;
        ${qwen35SelectorCase}
        gemma4-e4b)
          selected_model="${offlineModels.gemma4_e4b.id}"
          selected_config="${offlineConfigs.gemma4_e4b}"
          ;;
        qwen2.5-coder-7b)
          selected_model="${offlineModels.qwen2_5_coder_7b.id}"
          selected_config="${offlineConfigs.qwen2_5_coder_7b}"
          ;;
        qwen3-8b)
          selected_model="${offlineModels.qwen3_8b.id}"
          selected_config="${offlineConfigs.qwen3_8b}"
          ;;
        *)
          echo "unsupported local model selector: $RUGGED_OPENCODE_MODEL" >&2
          exit 2
          ;;
      esac
      selected_route="${offlineProvider}/$selected_model"
      backend_port="''${RUGGED_OPENCODE_BACKEND_PORT:-11436}"
      if [[ ! "$backend_port" =~ ^[0-9]+$ ]] || (( backend_port < 1 || backend_port > 65535 )); then
        echo "invalid RUGGED_OPENCODE_BACKEND_PORT: $backend_port" >&2
        exit 2
      fi

      mkdir -p "$(dirname "$OPENCODE_CONFIG")"
      cp "$selected_config" "$OPENCODE_CONFIG"
      ${pkgs.gnused}/bin/sed -i \
        "s|http://127.0.0.1:11436/v1|http://127.0.0.1:$backend_port/v1|g" \
        "$OPENCODE_CONFIG"

      set +e
      if [[ "''${1:-}" == acp || "''${1:-}" == models ]]; then
        ${pkgsUnstable.opencode}/bin/opencode "$@"
      else
        ${pkgsUnstable.opencode}/bin/opencode --model "$selected_route" --agent local-build "$@"
      fi
      result=$?
      set -e
      exit "$result"
    '';
  };

  offlineLauncher = pkgs.writeShellApplication {
    name = "rugged-opencode-offline";
    runtimeInputs = [
      pkgs.bubblewrap
      pkgs.coreutils
      pkgs.git
    ];
    text = ''
      if [[ -z "''${PWD:-}" ]]; then
        echo "run this from the project directory you want OpenCode to edit" >&2
        exit 2
      fi
      workspace="$(pwd -P)"
      backend_port="''${RUGGED_OPENCODE_BACKEND_PORT:-11436}"

      git_dir="$(git -C "$workspace" rev-parse --absolute-git-dir 2>/dev/null || true)"
      git_common_dir="$(git -C "$workspace" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
      bwrap_args=(
        ${pkgs.bubblewrap}/bin/bwrap
        --ro-bind / /
        --dev /dev
        --proc /proc
        --tmpfs /home
        --tmpfs /root
        --tmpfs /tmp
        --dir /tmp/offline-workspace
        --bind "$workspace" /tmp/offline-workspace
        --dir /tmp/offline-home
        --dir /tmp/offline-home/.config
        --dir /tmp/offline-home/.cache
        --dir /tmp/offline-home/.local
        --dir /tmp/offline-home/.local/share
        --dir /tmp/offline-home/.local/state
      )
      if [[ -n "$git_dir" && -n "$git_common_dir" ]]; then
        bwrap_args+=(
          --dir /tmp/offline-git
          --dir /tmp/offline-git/worktree
          --dir /tmp/offline-git/common
          --ro-bind "$git_dir" /tmp/offline-git/worktree
          --ro-bind "$git_common_dir" /tmp/offline-git/common
        )
      fi
      bwrap_args+=(
        --clearenv
        --setenv PATH "$PATH"
        --setenv HOME /tmp/offline-home
        --setenv XDG_CONFIG_HOME /tmp/offline-home/.config
        --setenv XDG_CACHE_HOME /tmp/offline-home/.cache
        --setenv XDG_DATA_HOME /tmp/offline-home/.local/share
        --setenv XDG_STATE_HOME /tmp/offline-home/.local/state
        --setenv TMPDIR /tmp
        --setenv LANG "''${LANG:-C.UTF-8}"
        --setenv TERM "''${TERM:-xterm-256color}"
        --setenv OPENCODE_CONFIG /tmp/offline-home/.config/opencode/opencode.json
        --setenv OPENCODE_DISABLE_AUTOUPDATE 1
        --setenv GIT_OPTIONAL_LOCKS 0
        --setenv RUGGED_OPENCODE_BACKEND_PORT "$backend_port"
        --setenv RUGGED_OPENCODE_MODEL "''${RUGGED_OPENCODE_MODEL:-qwen3-4b}"
      )
      if [[ -n "$git_dir" && -n "$git_common_dir" ]]; then
        bwrap_args+=(
          --setenv GIT_DIR /tmp/offline-git/worktree
          --setenv GIT_COMMON_DIR /tmp/offline-git/common
          --setenv GIT_WORK_TREE /tmp/offline-workspace
        )
      fi
      bwrap_args+=(
        --chdir /tmp/offline-workspace
        --
        ${offlineCommand}/bin/rugged-opencode-offline-inner
        "$@"
      )
      # TODO: Consider optional network isolation that still permits the selected local backend.
      echo "Local OpenCode: using host loopback ${backendLabel} port $backend_port; network access is not isolated." >&2
      if "''${bwrap_args[@]}"; then
        exit 0
      else
        exit "$?"
      fi
    '';
  };
in
{
  ducktape.opencode.providers = {
    # On-device model endpoint; the router is mutually exclusive with Ollama.
    rugged = {
      npm = "@ai-sdk/openai-compatible";
      name = if routerEnabled then "Rugged local llama.cpp router" else "Rugged local Ollama";
      options.baseURL = "http://127.0.0.1:11436/v1";
      models."gemma4:e2b-it-qat" = {
        name = "Gemma 4 E2B QAT (32k context; rugged iGPU)";
        reasoning = false;
        # Tool calls work (cluster/docs/inference/rugged_local_llm.md), but with
        # a real $HOME OpenCode's ~16k-token first prompt needs ~50 s of prefill
        # and once produced no output in 10 min. Stays off until OpenCode runs
        # with a minimal environment by default.
        tool_call = false;
        limit = {
          context = 32768;
          output = 8192;
        };
      };
    };

    # Gemma 4 on local Intel iGPU via Google LiteRT-LM. Start separately:
    #   litert-lm serve --host 127.0.0.1 --port 9379 --enable-speculative-decoding=true
    rugged-litert = {
      npm = "@ai-sdk/openai-compatible";
      name = "Rugged local LiteRT-LM";
      options.baseURL = "http://127.0.0.1:9379/v1";
      # The current Gemma 4 E2B LiteRT artifact rewrites its magic-number target
      # to 32000 tokens, so advertise that as the usable full context here rather
      # than Ollama's larger GGUF/QAT context.
      models."gemma4-e2b-it,gpu,32000" = {
        name = "Gemma 4 E2B LiteRT-LM MTP 32k (rugged iGPU)";
        reasoning = false;
        # The endpoint requires max_completion_tokens (max_tokens is ignored).
        # Three raw structured tool calls and a tool-result follow-up passed;
        # a full OpenCode agent task is still unverified, so keep calls disabled.
        tool_call = false;
        limit = {
          context = 32000;
          output = 8192;
        };
      };
    };
  };

  # This dedicated command has its own minimal OpenCode config and a fresh HOME,
  # while connecting directly to host-loopback Ollama. Shared OpenCode providers,
  # skills, and MCP servers remain owned by the normal profile.
  home.packages = [ offlineLauncher ];
}
