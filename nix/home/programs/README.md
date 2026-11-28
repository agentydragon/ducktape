# Home Manager program modules

Generic modules expose options; personal integrations select their values.
The [Gemini CLI module](gemini-cli.nix) extends Home Manager’s former Gemini module
with policy TOML files and shared skills. Its `policies` option includes an example;
[the personal integration](../gemini_cli.nix) turns the shared command lists into rules.
Home Manager itself owned the CLI package and settings file.

Both imports are disabled. See [home.nix](../home.nix) for the upstream rename
and what to check before enabling the integration again.
