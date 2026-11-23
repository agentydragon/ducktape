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
  disallowedTools ? [ ],
  maxContextTokens ? null,
  maxOutputTokens ? null,
}:
let
  # Claude wrapper budgets are authored in model_catalog/nix.py and serialized
  # in model_catalog/claude-wrappers.json. They are client settings, not provider
  # input ceilings or proof of backend capacity; Claude Code applies its own
  # model recognition, output reserve and compaction rules.
  #
  # gatewayDiscovery and the `[1m]` suffix convention (see litellm-claude.nix) don't compose.
  # `[1m]` is stripped from the outbound `model:` field before the request goes out — it
  # survives only as a client-side signal (1M context-window sizing, the
  # `context-1m-2025-08-07` beta header) — so it can only ever be a value baked into `model`/
  # `haikuModel` here, never a choice in the `/model` picker under gatewayDiscovery: the
  # gateway's `/v1/models` roster can only list names it actually routes, and a `[1m]`-suffixed
  # entry would be a slug added purely for one client's convention, routable to the exact same
  # backend as its unsuffixed twin. Concretely: litellm-claude hardcodes `[1m]` onto its
  # default Sonnet model, but switching to Opus or Fable mid-session via the discovered roster
  # loses 1M context — there's no way to get both a friendly in-session model picker and a 1M
  # variant of a model that isn't the wrapper's hardcoded default.
  envLines = [
    "IS_DEMO=1"
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
  runtimeInputs = [ pkgs.coreutils ];
  text = ''
    exec env -u ANTHROPIC_API_KEY \
      ${envBlock} \
      ${claudeLine}
  '';
}
