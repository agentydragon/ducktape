# OpenCode local-LLM provider wiring for rugged's on-device inference.
#
# TODO: activate the authenticated in-cluster serving route from PR #10452
# after its deployment is validated.
{
  pkgs,
  pkgsUnstable,
  ...
}:
let
  offlineProvider = "rugged-offline";
  offlineModels = {
    qwen3_4b = {
      id = "qwen3:4b-instruct-2507-q4_K_M";
      name = "Qwen3 4B Instruct (rugged iGPU, offline profile)";
      context = 32768;
      output = 4096;
      toolCall = true;
    };
    gemma4_e2b = {
      id = "gemma4:e2b-it-qat";
      name = "Gemma 4 E2B QAT (rugged iGPU, offline profile)";
      context = 131072;
      output = 8192;
      toolCall = true;
    };
    gemma4_e4b = {
      id = "gemma4:e4b";
      name = "Gemma 4 E4B (rugged iGPU, offline profile)";
      context = 32768;
      output = 4096;
      toolCall = true;
    };
  };
  offlineConfigFor =
    key: model:
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
          name = "Rugged offline Ollama";
          options = {
            baseURL = "http://127.0.0.1:11437/v1";
            timeout = false;
            headerTimeout = false;
            chunkTimeout = false;
          };
          models.${model.id} = {
            inherit (model) name;
            reasoning = false;
            temperature = true;
            tool_call = model.toolCall;
            limit = {
              inherit (model) context output;
            };
          };
        };
      }
    );
  offlineConfigs = builtins.mapAttrs offlineConfigFor offlineModels;

  offlineNamespaceCommand = pkgs.writeShellApplication {
    name = "rugged-opencode-offline-namespace";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.socat
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
        gemma4-e4b)
          selected_model="${offlineModels.gemma4_e4b.id}"
          selected_config="${offlineConfigs.gemma4_e4b}"
          ;;
        *)
          echo "unsupported local model selector: $RUGGED_OPENCODE_MODEL" >&2
          exit 2
          ;;
      esac
      selected_route="${offlineProvider}/$selected_model"

      mkdir -p "$(dirname "$OPENCODE_CONFIG")"
      cp "$selected_config" "$OPENCODE_CONFIG"

      ${pkgs.socat}/bin/socat TCP-LISTEN:11437,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/tmp/offline-bridge/backend.sock >/dev/null 2>&1 &
      relay_pid=$!
      trap 'kill "$relay_pid" 2>/dev/null || true; wait "$relay_pid" 2>/dev/null || true' EXIT
      ${pkgs.coreutils}/bin/sleep 0.1
      kill -0 "$relay_pid" 2>/dev/null || {
        echo "namespace loopback inference relay exited during startup" >&2
        exit 1
      }

      set +e
      ${pkgsUnstable.opencode}/bin/opencode --model "$selected_route" --agent local-build "$@"
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
      pkgs.socat
    ];
    text = ''
      if [[ -z "''${PWD:-}" ]]; then
        echo "run this from the project directory you want OpenCode to edit" >&2
        exit 2
      fi
      workspace="$(pwd -P)"
      backend_port="''${RUGGED_OPENCODE_BACKEND_PORT:-11436}"
      bridge_dir="$(mktemp -d "''${TMPDIR:-/tmp}"/rugged-opencode-bridge.XXXXXX)"
      chmod 700 "$bridge_dir"

      trap 'kill "''${relay_pid:-}" 2>/dev/null || true; wait "''${relay_pid:-}" 2>/dev/null || true; rm -rf -- "$bridge_dir"' EXIT

      ${pkgs.socat}/bin/socat \
        "UNIX-LISTEN:$bridge_dir/backend.sock,fork,unlink-early,mode=600" \
        "TCP:127.0.0.1:$backend_port" \
        >/dev/null 2>&1 &
      relay_pid=$!
      for _ in {1..50}; do
        [[ -S "$bridge_dir/backend.sock" ]] && break
        kill -0 "$relay_pid" 2>/dev/null || {
          echo "loopback inference relay exited during startup" >&2
          exit 1
        }
        sleep 0.1
      done
      [[ -S "$bridge_dir/backend.sock" ]] || {
        echo "loopback inference relay did not create its Unix socket" >&2
        exit 1
      }

      git_dir="$(git -C "$workspace" rev-parse --absolute-git-dir 2>/dev/null || true)"
      git_common_dir="$(git -C "$workspace" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
      bwrap_args=(
        ${pkgs.bubblewrap}/bin/bwrap
        --unshare-net
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
        --dir /tmp/offline-bridge
        --ro-bind "$bridge_dir/backend.sock" /tmp/offline-bridge/backend.sock
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
        ${offlineNamespaceCommand}/bin/rugged-opencode-offline-namespace
        "$@"
      )
      echo "Offline OpenCode: external network denied; only host loopback port $backend_port is bridged." >&2
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
    # Gemma 4 on local Intel iGPU via upstream Ollama/Vulkan.
    rugged = {
      npm = "@ai-sdk/openai-compatible";
      name = "Rugged local Ollama";
      options.baseURL = "http://127.0.0.1:11436/v1";
      models."gemma4:e2b-it-qat" = {
        name = "Gemma 4 E2B QAT (rugged iGPU)";
        reasoning = false;
        # Tool calls work (cluster/docs/inference/rugged_local_llm.md), but with
        # a real $HOME OpenCode's ~16k-token first prompt needs ~50 s of prefill
        # and once produced no output in 10 min. Stays off until OpenCode runs
        # with a minimal environment by default.
        tool_call = false;
        limit = {
          context = 131072;
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
  # denies external networking, and bridges only the configured loopback Ollama
  # port through a temporary Unix socket. Shared OpenCode providers, skills, and
  # MCP servers remain owned by the normal profile.
  home.packages = [ offlineLauncher ];
}
