# mkClaudeGateway — generate a Claude Code wrapper that points the CLI at an
# Anthropic-shaped gateway via Bearer auth. Owns the env/exec pattern once; each gateway
# (codex-claude, tana-claude, gemini-claude) is a declarative spec passed as an attrset.
#
# Always strips ANTHROPIC_API_KEY (`env -u`) so Claude Code never sees both
# ANTHROPIC_AUTH_TOKEN and ANTHROPIC_API_KEY set — it warns "auth may not work as expected"
# otherwise, and an inherited real Anthropic key (e.g. wyrm2's ducktape.sopsEnv) would
# trigger it even when the wrapper sets only the token. Packaged with writeShellApplication
# so every generated wrapper is shellcheck'd.
{ pkgs, lib }:
name:
{
  baseUrl,
  authTokenEnvVar,
  model,
  haikuModel ? null,
  gatewayDiscovery ? false,
  isDemo ? true,
  disallowedTools ? [ ],
  runtimeInputs ? [ ],
}:
let
  envLines =
    lib.optional isDemo "IS_DEMO=1"
    ++ [
      ''ANTHROPIC_BASE_URL="${baseUrl}"''
      ''ANTHROPIC_AUTH_TOKEN="${"$"}${authTokenEnvVar}"''
      ''ANTHROPIC_MODEL="${model}"''
    ]
    ++ lib.optional (haikuModel != null) ''ANTHROPIC_DEFAULT_HAIKU_MODEL="${haikuModel}"''
    ++ lib.optional gatewayDiscovery "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1";
  # ` \\\n  ` = space, backslash (line continuation), newline, 2-space indent.
  envBlock = lib.concatStringsSep " \\\n  " envLines;
  claudeLine =
    if disallowedTools == [ ] then
      ''claude "$@"''
    else
      ''claude --disallowed-tools "${lib.concatStringsSep " " disallowedTools}" "$@"'';
in
pkgs.writeShellApplication {
  inherit name;
  runtimeInputs = [ pkgs.coreutils ] ++ runtimeInputs;
  text = ''
    exec env -u ANTHROPIC_API_KEY \
      ${envBlock} \
      ${claudeLine}
  '';
}
