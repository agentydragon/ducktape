# OpenCode configuration for local Ollama models
#
# Best agentic models for 32GB VRAM (RTX 5090):
# - qwen3:32b - Best tool calling, recommended for agentic work
# - qwen2.5-coder:32b - Optimized for coding tasks
# - qwen2.5:32b-instruct - General purpose, good fallback
#
# To pull recommended model: ollama pull qwen3:32b
#
# Reasoning support:
# - Qwen3 has thinking mode (returns reasoning in reasoning_content field)
# - Set interleaved.field = "reasoning_content" to enable proper handling
{
  config,
  pkgs,
  lib,
  ...
}: let
  # OpenCode configuration as JSON
  # Docs: https://opencode.ai/docs/providers/
  opencodeConfig = {
    "$schema" = "https://opencode.ai/config.json";
    provider = {
      ollama = {
        npm = "@ai-sdk/openai-compatible";
        name = "Ollama (local)";
        options = {
          baseURL = "http://localhost:11434/v1";
        };
        models = {
          # Primary: Qwen3 32B with 32k context - best for agentic/tool-calling work
          # Has thinking mode - reasoning returned in reasoning_content field
          "qwen3:32b-32k" = {
            name = "Qwen3 32B 32k (local)";
            reasoning = true;
            tool_call = true;
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 32768;
              output = 8192;
            };
          };
          # Base Qwen3 32B (4k default context)
          "qwen3:32b" = {
            name = "Qwen3 32B (local)";
            reasoning = true;
            tool_call = true;
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 4096;
              output = 8192;
            };
          };
          # DeepSeek R1 - reasoning model with thinking mode
          "deepseek-r1:32b" = {
            name = "DeepSeek R1 32B (local)";
            reasoning = true;
            tool_call = true;
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 131072;
              output = 8192;
            };
          };
        };
      };
    };
  };
in {
  # Write opencode.json to ~/.config/opencode/
  xdg.configFile."opencode/opencode.json" = {
    text = builtins.toJSON opencodeConfig;
  };
}
