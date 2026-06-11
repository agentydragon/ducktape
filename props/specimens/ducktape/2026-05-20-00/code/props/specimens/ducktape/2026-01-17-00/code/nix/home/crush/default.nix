# Crush LLM tool configuration
# https://charm.land/crush
{
  config,
  pkgs,
  lib,
  ...
}: {
  # ~/.config/crush/crush.json
  xdg.configFile."crush/crush.json".text = builtins.toJSON {
    "$schema" = "https://charm.land/crush.json";
    mcp = {};
    options = {
      context_paths = [];
      diff = {
        external_command = "git diff --no-index --histogram --minimal --word-diff=porcelain -U3 -- a {old} -- b {new}";
        parse_mode = "git_word_porcelain";
      };
      data_directory = "/home/agentydragon/.crush";
      debug = true;
      debug_provider_wire = true;
      wire = {
        debug_mcp_wire = true;
        mcp_log_mode = "per_server";
        mcp_filename = "mcp-wire.log";
        max_size_mb = 250;
        max_backups = 10;
        max_age_days = 30;
        compress = true;
      };
      mcp = {
        tool_timeout_secs = 300;
      };
      reasoning_summary = "detailed";
      show_reasoning_summaries = true;
    };
    providers = {
      openai = {
        generation_api = "responses";
      };
    };
  };

  # ~/.local/share/crush/crush.json
  xdg.dataFile."crush/crush.json".text = builtins.toJSON {
    models = {
      large = {
        model = "gpt-5";
        provider = "openai";
        reasoning_effort = "high";
        max_tokens = 272000;
      };
      small = {
        model = "gpt-5-mini";
        provider = "openai";
        max_tokens = 272000;
      };
    };
    providers = {
      openai = {
        generation_api = "responses";
      };
    };
  };
}
