# OpenCode configuration for local LLM inference (Ollama + vLLM)
#
# Hardware: 2x RTX 5090 (64GB total VRAM)
#
# RECOMMENDED: vLLM with AWQ quantization
#   Start: ~/code/ducktape/experimental/local-llm/start-vllm-awq.sh
#   Model: qwen3-coder-awq (262K context, 8.5 GiB/GPU, FP8 KV cache)
#
# Critical vLLM fixes (see qwen3-coder-vram-analysis.md):
#   - --max-num-seqs 32 (default 256 causes OOM during warmup)
#   - --kv-cache-dtype fp8 (doubles context capacity)
#   - Don't use --quantization awq (model auto-detects compressed-tensors)
#
# Two inference backends:
#   - vLLM (port 8000): Tensor parallelism, 262K context, recommended
#   - Ollama (port 11434): Easy setup, GGUF quantization
#
# Capability matrix:
#   Model                         | Reasoning | Tools | Context | Size/GPU | Notes
#   ------------------------------|-----------|-------|---------|----------|---------------------------
#   === CONFIGURED (vLLM) ===
#   deepseek-r1-32b               | ✓         | ✓     | 128k    | ~17 GB   | Best reasoning 32B, start-vllm-deepseek-r1.sh
#   deepseek-r1-70b               | ✓         | ✓     | 64k     | ~38 GB   | Best quality, start-vllm-deepseek-r1-70b.sh
#   qwen3-32b                     | ✓         | ✓     | 40k     | ~17 GB   | General model, start-vllm-qwen3-32b.sh
#   qwen3-coder-awq               | ✗         | ✓     | 262k    | ~8.5 GB  | AWQ removes thinking, start-vllm-awq.sh
#   === CONFIGURED (Ollama) ===
#   qwen3-coder-long              | ✗*        | ✓     | 131k    | ~19 GB   | *thinking untested
#   llama3.3:70b                  | ✗         | ✓     | 32k     | ~38 GB   | Reliable tools, no thinking
#   === DOWNLOADED (not yet configured) ===
#   deepseek-r1-distill-llama-70b | ✓         | ✓     | 128k    | ~19 GB   | Best quality, needs TP=2
#   qwen3-32b-awq                 | ✓         | ✓     | 128k    | ~17 GB   | General model, thinking works
#
# See model-download-list.md for download status and benchmarks.
{
  config,
  pkgs,
  lib,
  siderolabs-docs,
  skills-tar,
  ...
}:
let
  # OpenCode configuration as JSON
  # Docs: https://opencode.ai/docs/providers/
  opencodeConfig = {
    "$schema" = "https://opencode.ai/config.json";
    provider = {
      # === vLLM: Tensor parallelism for better throughput ===
      # Start server: ~/code/ducktape/experimental/local-llm/start-vllm-awq.sh
      vllm = {
        npm = "@ai-sdk/openai-compatible";
        name = "vLLM (local, tensor parallel)";
        options = {
          baseURL = "http://0.0.0.0:8000/v1";
        };
        models = {
          # Qwen3-Coder 30B AWQ 4-bit with tensor parallelism across 2x 5090
          # AWQ quantization: ~8.5 GB/GPU weights (vs 28.5 GB bf16)
          # FP8 KV cache: ~23 GB available per GPU = 262K context
          # ⚠️ AWQ model does NOT support thinking mode (per model card)
          # See: experimental/local-llm/qwen3-coder-vram-analysis.md
          "qwen3-coder-awq" = {
            name = "Qwen3-Coder 30B AWQ (vLLM)";
            reasoning = false; # AWQ model removes thinking support
            tool_call = true;
            limit = {
              context = 262144;
              output = 8192;
            };
          };

          # DeepSeek R1 Distill Qwen 32B - reasoning + tools preserved
          # Distilled from DeepSeek-R1, maintains thinking capability
          # Start: ~/code/ducktape/experimental/local-llm/start-vllm-deepseek-r1.sh
          "deepseek-r1-32b" = {
            name = "DeepSeek R1 Distill Qwen 32B (vLLM)";
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

          # DeepSeek R1 Distill Llama 70B - best quality distillation
          # Requires TP=2 (both GPUs), ~38 GB total
          # Start: ~/code/ducktape/experimental/local-llm/start-vllm-deepseek-r1-70b.sh
          "deepseek-r1-70b" = {
            name = "DeepSeek R1 Distill Llama 70B (vLLM)";
            reasoning = true;
            tool_call = true;
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 65536; # Reduced to fit 64GB with 38GB weights
              output = 8192;
            };
          };

          # Qwen3 32B AWQ - general model with thinking + tools
          # Good all-around model, not code-specialized
          # Start: ~/code/ducktape/experimental/local-llm/start-vllm-qwen3-32b.sh
          "qwen3-32b" = {
            name = "Qwen3 32B AWQ (vLLM)";
            reasoning = true;
            tool_call = true;
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 40960; # Qwen3-32B-AWQ native limit
              output = 8192;
            };
          };
        };
      };

      # === Cluster: GPT-OSS via LiteLLM at litellm.allegedly.works ===
      # Select in UI: /model → pick cluster/gpt-oss-20b-128k or cluster/gpt-oss-120b-128k
      # ⚠️ gpt-oss streaming bug may apply (finishReason as object).
      # See: https://github.com/anomalyco/opencode/issues/7439
      cluster = {
        npm = "@ai-sdk/openai-compatible";
        name = "Cluster (litellm.allegedly.works)";
        options = {
          baseURL = "https://litellm.allegedly.works/v1";
          apiKey = "{env:OLLAMA_API_KEY}";
        };
        models = {
          "gpt-oss-20b-128k" = {
            name = "GPT-OSS 20B 128k (cluster)";
            reasoning = true;
            tool_call = true;
            limit = {
              context = 131072;
              output = 8192;
            };
          };
          "gpt-oss-120b-128k" = {
            name = "GPT-OSS 120B 128k (cluster)";
            reasoning = true;
            tool_call = true;
            limit = {
              context = 131072;
              output = 8192;
            };
          };
        };
      };

      # === Ollama: Easy setup, GGUF quantization ===
      ollama = {
        # NOTE: gpt-oss models have streaming response format issues with OpenCode
        # The finishReason is returned as object instead of string
        # See: https://github.com/anomalyco/opencode/issues/7439
        npm = "@ai-sdk/openai-compatible";
        name = "Ollama (local)";
        options = {
          baseURL = "http://localhost:11434/v1";
        };
        models = {
          # === GPT-OSS - DISABLED: OpenCode streaming compatibility issue ===
          # OpenAI's open-weight MoE models (Apache 2.0, reasoning + tools)
          # Docs: https://ollama.com/library/gpt-oss
          #
          # BUG: gpt-oss returns finishReason as object instead of string in
          # streaming responses, causing ZodError in OpenCode's processor.ts.
          # Affects all providers (@ai-sdk/openai-compatible, ollama-ai-provider-v2).
          # Issue: https://github.com/anomalyco/opencode/issues/7439
          #
          # Re-enable once OpenCode fixes streaming response parsing.
          #
          # "gpt-oss-120b-32k" = {
          #   name = "GPT-OSS 120B 32k (local)";
          #   # MoE: 117B params, 5.1B active, ~56GB MXFP4. Fits 2x5090 w/ 32k ctx.
          #   # Create variant: ollama run gpt-oss:120b → /set parameter num_ctx 32768 → /save gpt-oss-120b-32k
          #   reasoning = true;
          #   tool_call = true;
          #   limit = { context = 32768; output = 8192; };
          # };
          # "gpt-oss-20b-32k" = {
          #   name = "GPT-OSS 20B 32k (local)";
          #   # MoE: 14GB weights, fits easily on 64GB with large context.
          #   # Create variant: ollama run gpt-oss:20b → /set parameter num_ctx 32768 → /save gpt-oss-20b-32k
          #   reasoning = true;
          #   tool_call = true;
          #   limit = { context = 32768; output = 8192; };
          # };

          # === Qwen3-Coder - BOTH reasoning AND reliable tool calling ===

          # Qwen3-Coder 30B with 131k context - recommended for large codebases
          # Q4_K_M (19GB) + FP16 KV cache supports ~218k context on 2x5090
          # See: experimental/local-llm/qwen3-coder-vram-analysis.md
          # Create variant:
          #   cd ~/code/ducktape/experimental/local-llm
          #   ollama create qwen3-coder-long -f Modelfile.qwen3-coder-long
          "qwen3-coder-long" = {
            name = "Qwen3-Coder 30B 131k (local)";
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

          # Qwen3-Coder 30B with 32k context - smaller memory footprint
          # Unsloth fixed tool calling in Aug 2025
          # Create variant:
          #   ollama run qwen3-coder:30b
          #   /set parameter num_ctx 32768
          #   /save qwen3-coder-30b-32k
          #   /bye
          "qwen3-coder-30b-32k" = {
            name = "Qwen3-Coder 30B 32k (local)";
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

          # === Qwen3 - reasoning works, tools BUGGY in Ollama ===

          # Qwen3 32B with 32k context
          # WARNING: Tool calling has parsing issues in Ollama
          "qwen3:32b-32k" = {
            name = "Qwen3 32B 32k (local)";
            reasoning = true;
            tool_call = true; # unreliable
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 32768;
              output = 8192;
            };
          };
          # Base Qwen3 32B (4k default context)
          # WARNING: Tool calling has parsing issues in Ollama
          "qwen3:32b" = {
            name = "Qwen3 32B (local)";
            reasoning = true;
            tool_call = true; # unreliable
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 4096;
              output = 8192;
            };
          };
          # DeepSeek R1 32B - disabled: does not support tool calling
          # "deepseek-r1:32b" = {
          #   name = "DeepSeek R1 32B (local)";
          #   reasoning = true;
          #   tool_call = true;
          #   interleaved = {
          #     field = "reasoning_content";
          #   };
          #   limit = {
          #     context = 131072;
          #     output = 8192;
          #   };
          # };
          # Qwen3 abliterated (uncensored) variant
          # WARNING: Tool calling has parsing issues in Ollama
          "huihui_ai/qwen3-abliterated:32b" = {
            name = "Qwen3 32B Abliterated (local)";
            reasoning = true;
            tool_call = true; # unreliable
            interleaved = {
              field = "reasoning_content";
            };
            limit = {
              context = 40960; # model's native context; fits in 32GB VRAM
              output = 8192;
            };
          };

          # === 70B models (require 2x 5090 / 64GB VRAM) ===
          # === Llama - RELIABLE tools, NO reasoning/thinking ===

          # Llama 3.3 70B - best overall for tool use, matches 405B performance
          "llama3.3:70b" = {
            name = "Llama 3.3 70B (local)";
            reasoning = false;
            tool_call = true;
            limit = {
              context = 32768; # safe limit; model supports 128k native
              output = 8192;
            };
          };
          # Llama 3.3 70B with extended context (no reasoning)
          "llama3.3:70b-64k" = {
            name = "Llama 3.3 70B 64k (local)";
            reasoning = false;
            tool_call = true;
            limit = {
              context = 65536; # aggressive but fits in 64GB with Q4
              output = 8192;
            };
          };

          # DeepSeek R1 70B - disabled: Ollama lacks tool calling templates
          # Use MFDoom/deepseek-r1-tool-calling:70b for tool support
          # "deepseek-r1:70b" = {
          #   name = "DeepSeek R1 70B (local)";
          #   reasoning = true;
          #   tool_call = true;
          #   interleaved = {
          #     field = "reasoning_content";
          #   };
          #   limit = {
          #     context = 32768;  # safe limit; model supports 128k native
          #     output = 8192;
          #   };
          # };
          # "deepseek-r1:70b-64k" = {
          #   name = "DeepSeek R1 70B 64k (local)";
          #   reasoning = true;
          #   tool_call = true;
          #   interleaved = {
          #     field = "reasoning_content";
          #   };
          #   limit = {
          #     context = 65536;  # aggressive but fits in 64GB with Q4
          #     output = 8192;
          #   };
          # };

          # Llama 3.1 70B Abliterated - uncensored, reliable tools, no reasoning
          # Pull via: ollama pull krith/meta-llama-3.1-70b-instruct-abliterated:IQ3_M
          "krith/meta-llama-3.1-70b-instruct-abliterated:IQ3_M" = {
            name = "Llama 3.1 70B Abliterated (local)";
            reasoning = false; # no thinking mode
            tool_call = true; # reliable
            limit = {
              context = 32768; # safe limit; model supports 128k native
              output = 8192;
            };
          };
        };
      };
    };
  };
in
{
  # Write opencode.json to ~/.config/opencode/
  xdg.configFile."opencode/opencode.json" = {
    text = builtins.toJSON opencodeConfig;
  };

  # Deploy skills to ~/.config/opencode/skills/ (shared with Claude Code, Gemini CLI)
  home.file = (import ../skills/skills.nix { inherit lib pkgs siderolabs-docs skills-tar; }) ".config/opencode";
}
