# OpenCode local-LLM provider wiring for rugged's on-device inference.
#
# TODO: expose this through an authenticated in-cluster route if rugged's
# local LLM becomes useful beyond the tablet itself.
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
        # LiteRT-LM's OpenAI handler accepts the tools envelope, but tool use
        # and output limiting are not reliable enough for OpenCode yet.
        tool_call = false;
        limit = {
          context = 32000;
          output = 8192;
        };
      };
    };
  };
}
