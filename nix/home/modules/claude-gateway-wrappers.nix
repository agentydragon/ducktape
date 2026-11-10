{ lib, ... }:
{
  options.ducktape.claudeGatewayWrappers = lib.mkOption {
    type = lib.types.listOf (
      lib.types.enum [
        "codex-claude"
        "tana-claude"
        "gemini-claude"
        "antigravity-claude"
        "litellm-claude"
      ]
    );
    default = [ ];
    description = ''
      Claude gateway wrappers to install with their SOPS client keys.
      tana-claude remains parked: its renderer deliberately fails until its
      routes and client key are restored.
    '';
  };
}
