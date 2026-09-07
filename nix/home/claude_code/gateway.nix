# mkClaudeGateway — generate a Claude Code wrapper that points the CLI at an
# Anthropic-shaped gateway via Bearer auth. Owns the env/exec pattern once; each gateway
# (codex-claude, tana-claude, gemini-claude) is a declarative spec passed as an attrset.
#
# The bearer token is read from its sops-nix secret file at exec time rather than from an
# exported variable, so the credential lives in the one process that needs it instead of in
# every shell and everything a shell spawns. `authTokenFile` is a `config.sops.secrets.<name>.path`.
#
# Always strips ANTHROPIC_API_KEY (`env -u`) so Claude Code never sees both
# ANTHROPIC_AUTH_TOKEN and ANTHROPIC_API_KEY set — it warns "auth may not work as expected"
# otherwise, and an inherited real Anthropic key would trigger it even when the wrapper sets
# only the token. Packaged with writeShellApplication so every generated wrapper is shellcheck'd.
{ pkgs, lib }:
name:
{
  baseUrl,
  authTokenFile,
  model,
  haikuModel ? null,
  gatewayDiscovery ? false,
  isDemo ? true,
  disallowedTools ? [ ],
  runtimeInputs ? [ ],
  maxContextTokens ? null,
  maxOutputTokens ? null,
}:
let
  # Gateway model slugs (e.g. `chatgpt/ant-messages/gpt-5.6-luna`) are not names Claude Code
  # recognizes, so without maxContextTokens it assumes a 200k window and auto-compacts against
  # it — clipping a larger real window or (for Gemini's ~1M) discarding most of it. Set
  # maxContextTokens to the model's real window so compaction math is correct; the value's SSOT
  # is cluster/k8s/litellm/app/model_rosters.py (CODEX_CONTEXT_WINDOW / GEMINI_CONTEXT_WINDOW) —
  # keep them in sync. maxOutputTokens caps output below the model's real max.
  envLines =
    lib.optional isDemo "IS_DEMO=1"
    ++ [
      ''ANTHROPIC_BASE_URL="${baseUrl}"''
      ''ANTHROPIC_AUTH_TOKEN="${"$"}(cat ${authTokenFile})"''
      ''ANTHROPIC_MODEL="${model}"''
    ]
    ++ lib.optional (haikuModel != null) ''ANTHROPIC_DEFAULT_HAIKU_MODEL="${haikuModel}"''
    ++ lib.optional gatewayDiscovery "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1"
    ++ lib.optional (
      maxContextTokens != null
    ) "CLAUDE_CODE_MAX_CONTEXT_TOKENS=${toString maxContextTokens}"
    ++ lib.optional (
      maxOutputTokens != null
    ) "CLAUDE_CODE_MAX_OUTPUT_TOKENS=${toString maxOutputTokens}";
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
