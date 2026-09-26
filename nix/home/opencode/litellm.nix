# Wires the cluster LiteLLM proxy's subscription-account routes into OpenCode as
# additional providers, reusing the SAME scoped virtual keys the Claude Code wrappers
# already use (litellm_codex_key, litellm_claude_subscription_key, litellm_gemini_key,
# litellm_antigravity_key -- declared in ../home.nix's sops.secrets). Never
# ducktape.sopsEnv: that exports to every shell and everything it spawns; OpenCode's
# own `opencode.json` resolves `{env:VAR}` from whatever process env `opencode` itself
# starts with, so this wraps the `opencode` binary the same way codex-claude/gemini-claude
# wrap `claude` -- one process holds the credential, not the whole session.
#
# Deliberately excluded:
# - Tana (tana_client_models): no verified context window/max tokens anywhere -- CLIProxyAPI's
#   own model registry (below) has no "tana" bucket at all, since Tana isn't a CLIProxyAPI
#   provider; it's litellm's own in-process adapter. Add once a real source turns up.
# - Self-hosted Ollama via litellm: redundant with the direct local vLLM/Ollama providers
#   already configured per-host (rugged-opencode.nix, wyrm2-opencode.nix) -- routing local
#   inference through the cluster proxy over the internet buys nothing.
# - Mistral/Groq: plain API-key passthroughs, not subscription accounts litellm mediates
#   access to; a user who wants them can configure their own keys directly in OpenCode.
#
# Context window / max tokens sourcing:
# - Codex (gpt-6-astra/luna/sol, gpt-5.6-luna/terra/sol): CODEX_CONTEXT_WINDOW/MAX_TOKENS
#   and ASTRA_CONTEXT_WINDOW/MAX_TOKENS (cluster/cdk8s/model_rosters.py) -- Astra measured
#   via Codex's own bundled metadata, the 5.6 family via a live probe
#   (openai_utils/probe_context_window.py) against this exact serving path. gpt-5.4/gpt-5.5
#   are excluded there for the same reason they're excluded here: never probed.
# - Gemini (gemini-3.7-flash, gemini-3.5-flash-lite): GEMINI_CONTEXT_WINDOW/MAX_OUTPUT_TOKENS
#   (model_rosters.py) -- Google's published spec for the direct AI-Studio-key path.
# - Claude subscription (claude-opus-5/sonnet-5/fable-5/haiku-4-5): from
#   third_party/cli_proxy_api's vendored CLIProxyAPI source (github.com/router-for-me/CLIProxyAPI,
#   pinned commit 7fac6b15bcfe), internal/registry/models/models.json's "claude" bucket
#   (checked 2026-09-26) -- 1,000,000/128,000 for Opus/Sonnet/Fable 5, 200,000/64,000 for
#   Haiku 4.5. Cross-checked against Anthropic's own published docs
#   (platform.claude.com/docs/en/build-with-claude/context-windows): matches exactly.
# - Antigravity (10 of 12 models): same registry's "antigravity" bucket, already cited in
#   cluster/cdk8s/model_rosters.py's ANTIGRAVITY_MODELS. gemini-3.1-flash-image and
#   gemini-3.5-flash-lite are missing from that file entirely, so excluded here too,
#   matching public-coder-agent's OpenClaw catalog (cluster/cdk8s/public_coder_agent_config.py).
{
  config,
  pkgs,
  pkgsUnstable,
  ...
}:
let
  _LITELLM_BASE = "https://litellm.allegedly.works/v1";

  opencode = pkgs.writeShellApplication {
    name = "opencode";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      exec env \
        LITELLM_CODEX_KEY="$(cat ${config.sops.secrets.litellm_codex_key.path})" \
        LITELLM_CLAUDE_SUBSCRIPTION_KEY="$(cat ${config.sops.secrets.litellm_claude_subscription_key.path})" \
        LITELLM_GEMINI_KEY="$(cat ${config.sops.secrets.litellm_gemini_key.path})" \
        LITELLM_ANTIGRAVITY_KEY="$(cat ${config.sops.secrets.litellm_antigravity_key.path})" \
        ${pkgsUnstable.opencode}/bin/opencode "$@"
    '';
  };
in
{
  ducktape.opencode.providers = {
    litellm-codex = {
      npm = "@ai-sdk/openai-compatible";
      name = "Codex subscription (LiteLLM)";
      options.baseURL = _LITELLM_BASE;
      options.apiKey = "{env:LITELLM_CODEX_KEY}";
      models = {
        "chatgpt/ant-messages/gpt-6-astra" = {
          name = "GPT-6 Astra (Codex subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 872000;
            output = 128000;
          };
        };
        "chatgpt/ant-messages/gpt-6-sol" = {
          name = "GPT-6 Sol (Codex subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 372000;
            output = 128000;
          };
        };
        "chatgpt/ant-messages/gpt-6-luna" = {
          name = "GPT-6 Luna (Codex subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 372000;
            output = 128000;
          };
        };
        "chatgpt/ant-messages/gpt-5.6-sol" = {
          name = "GPT-5.6 Sol (Codex subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 372000;
            output = 128000;
          };
        };
        "chatgpt/ant-messages/gpt-5.6-terra" = {
          name = "GPT-5.6 Terra (Codex subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 372000;
            output = 128000;
          };
        };
        "chatgpt/ant-messages/gpt-5.6-luna" = {
          name = "GPT-5.6 Luna (Codex subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 372000;
            output = 128000;
          };
        };
      };
    };

    litellm-claude-subscription = {
      npm = "@ai-sdk/openai-compatible";
      name = "Claude subscription (LiteLLM)";
      options.baseURL = _LITELLM_BASE;
      options.apiKey = "{env:LITELLM_CLAUDE_SUBSCRIPTION_KEY}";
      models = {
        "anthropic-max20/ant-messages/claude-opus-5" = {
          name = "Claude Opus 5 (subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1000000;
            output = 128000;
          };
        };
        "anthropic-max20/ant-messages/claude-sonnet-5" = {
          name = "Claude Sonnet 5 (subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1000000;
            output = 128000;
          };
        };
        "anthropic-max20/ant-messages/claude-fable-5" = {
          name = "Claude Fable 5 (subscription via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1000000;
            output = 128000;
          };
        };
        "anthropic-max20/ant-messages/claude-haiku-4-5-20251001" = {
          # Haiku models don't expose extended thinking in Anthropic's lineup.
          name = "Claude Haiku 4.5 (subscription via LiteLLM)";
          reasoning = false;
          tool_call = true;
          limit = {
            context = 200000;
            output = 64000;
          };
        };
      };
    };

    litellm-gemini = {
      npm = "@ai-sdk/openai-compatible";
      name = "Gemini (LiteLLM)";
      options.baseURL = _LITELLM_BASE;
      options.apiKey = "{env:LITELLM_GEMINI_KEY}";
      models = {
        "google/goog-generate/gemini-3.7-flash" = {
          name = "Gemini 3.7 Flash (Google AI via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65536;
          };
        };
        "google/goog-generate/gemini-3.5-flash-lite" = {
          name = "Gemini 3.5 Flash-Lite (Google AI via LiteLLM)";
          reasoning = false;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65536;
          };
        };
      };
    };

    litellm-antigravity = {
      npm = "@ai-sdk/openai-compatible";
      name = "Google Antigravity (LiteLLM)";
      options.baseURL = _LITELLM_BASE;
      options.apiKey = "{env:LITELLM_ANTIGRAVITY_KEY}";
      models = {
        "antigravity/ant-messages/claude-opus-4-6-thinking" = {
          name = "Claude Opus 4.6 (Thinking) (Google Antigravity via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 200000;
            output = 64000;
          };
        };
        "antigravity/ant-messages/claude-sonnet-4-6" = {
          name = "Claude Sonnet 4.6 (Thinking) (Google Antigravity via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 200000;
            output = 64000;
          };
        };
        "antigravity/ant-messages/gemini-3.6-flash-high" = {
          name = "Gemini 3.6 Flash (Google Antigravity via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65536;
          };
        };
        "antigravity/ant-messages/gemini-3.7-flash-high" = {
          name = "Gemini 3.7 Flash (Google Antigravity via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65536;
          };
        };
        "antigravity/ant-messages/gemini-3.8-flash-high" = {
          name = "Gemini 3.8 Flash (Google Antigravity via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65536;
          };
        };
        "antigravity/ant-messages/gemini-3-flash" = {
          name = "Gemini 3 Flash (Google Antigravity via LiteLLM)";
          reasoning = false;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65536;
          };
        };
        "antigravity/ant-messages/gemini-pro-agent" = {
          name = "Gemini 3.1 Pro (High) (Google Antigravity via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65535;
          };
        };
        "antigravity/ant-messages/gemini-3.1-pro-low" = {
          name = "Gemini 3.1 Pro (Low) (Google Antigravity via LiteLLM)";
          reasoning = false;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65535;
          };
        };
        "antigravity/ant-messages/gpt-oss-120b-medium" = {
          name = "GPT-OSS 120B (Medium) (Google Antigravity via LiteLLM)";
          reasoning = true;
          tool_call = true;
          limit = {
            context = 114000;
            output = 32768;
          };
        };
        "antigravity/ant-messages/gemini-3.1-flash-lite" = {
          name = "Gemini 3.1 Flash Lite (Google Antigravity via LiteLLM)";
          reasoning = false;
          tool_call = true;
          limit = {
            context = 1048576;
            output = 65535;
          };
        };
      };
    };
  };

  home.packages = [ opencode ];
}
