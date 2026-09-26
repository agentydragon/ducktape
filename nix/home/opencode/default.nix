# OpenCode configuration (https://opencode.ai/docs/providers/).
#
# This module owns the shared defaults (skills, grocy MCP) plus a typed
# `providers` extension point. Host-specific local-LLM wiring belongs in that
# host's own config (e.g. nix/home/hosts/wyrm2-opencode.nix), which sets
# `ducktape.opencode.providers.<name> = { ... }` — this module never needs to
# know which host has which GPU.
{
  config,
  lib,
  sharedSkillsArgs,
  ...
}:
let
  mkSkills = import ../skills.nix sharedSkillsArgs;
  cfg = config.ducktape.opencode;

  opencodeModelType = lib.types.submodule {
    options = {
      name = lib.mkOption {
        type = lib.types.str;
        description = "Display name shown in the OpenCode model picker.";
      };
      reasoning = lib.mkOption {
        type = lib.types.bool;
        description = "Whether this model exposes a thinking/reasoning channel.";
      };
      tool_call = lib.mkOption {
        type = lib.types.bool;
        description = "Whether this model supports OpenCode tool calling.";
      };
      interleaved = lib.mkOption {
        type = lib.types.nullOr (
          lib.types.submodule {
            options.field = lib.mkOption {
              type = lib.types.str;
              description = "Response field carrying interleaved reasoning content.";
            };
          }
        );
        default = null;
        description = "Interleaved-reasoning wiring, for models that stream a separate reasoning field.";
      };
      limit = lib.mkOption {
        type = lib.types.submodule {
          options = {
            context = lib.mkOption {
              type = lib.types.ints.positive;
              description = "Max context tokens OpenCode will send to this model.";
            };
            output = lib.mkOption {
              type = lib.types.ints.positive;
              description = "Max output tokens OpenCode will request from this model.";
            };
          };
        };
        description = "Context/output token limits OpenCode enforces for this model.";
      };
    };
  };

  opencodeProviderType = lib.types.submodule {
    options = {
      npm = lib.mkOption {
        type = lib.types.str;
        description = "npm package implementing this provider's AI SDK adapter.";
      };
      name = lib.mkOption {
        type = lib.types.str;
        description = "Display name shown in the OpenCode provider picker.";
      };
      options = lib.mkOption {
        type = lib.types.submodule {
          options.baseURL = lib.mkOption {
            type = lib.types.str;
            description = "OpenAI-compatible base URL for this provider's server.";
          };
        };
        description = "Provider-level options passed to the AI SDK adapter.";
      };
      models = lib.mkOption {
        type = lib.types.attrsOf opencodeModelType;
        default = { };
        description = "Models exposed under this provider, keyed by model id.";
      };
    };
  };

  # Drop attrset values assigned `null` (e.g. an unset `interleaved`) so they
  # never render as a literal `"key": null` in opencode.json.
  stripNulls =
    value:
    if builtins.isAttrs value then
      lib.mapAttrs (_: stripNulls) (lib.filterAttrs (_: v: v != null) value)
    else if builtins.isList value then
      map stripNulls value
    else
      value;

  opencodeConfig = stripNulls {
    "$schema" = "https://opencode.ai/config.json";
    mcp = {
      "grocy-sf" = {
        type = "remote";
        url = "https://grocy-mcp-sf.allegedly.works/mcp";
      };
      "grocy-vallejo" = {
        type = "remote";
        url = "https://grocy-mcp-vallejo.allegedly.works/mcp";
      };
    };
    provider = cfg.providers;
  };
in
{
  options.ducktape.opencode.providers = lib.mkOption {
    type = lib.types.attrsOf opencodeProviderType;
    default = { };
    description = ''
      OpenCode provider entries (https://opencode.ai/docs/providers/), keyed by
      provider id. Host configs contribute their own local-LLM wiring here
      instead of this module hardcoding per-host hardware.
    '';
  };

  config = {
    # Write opencode.json to ~/.config/opencode/
    xdg.configFile."opencode/opencode.json" = {
      text = builtins.toJSON opencodeConfig;
    };

    # Deploy skills to ~/.config/opencode/skills/ (shared with Claude Code, Gemini CLI)
    home.file = mkSkills { prefix = ".config/opencode"; };
  };
}
