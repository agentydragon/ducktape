# Known SSH public keys, sourced from ssh_keys/*.pub files.
# Import as: let keys = import ./ssh_keys.nix; in ...
let
  readKey =
    file: builtins.replaceStrings [ "\n" ] [ "" ] (builtins.readFile (../ssh_keys + "/${file}"));
in
{
  iguana = readKey "iguana-default.pub";
  wyrm2 = readKey "wyrm2-default.pub";
  rugged = readKey "rugged-default.pub";
  rugged_wyrm = readKey "rugged-wyrm.pub";
  atlas = readKey "atlas-default.pub";
  gecko = readKey "gecko-default.pub";
  publicCoderDevbox = readKey "public-coder-devbox.pub";
  publicCoderAgentSshpiper = readKey "public-coder-agent-sshpiper.pub";
  wyrm2McpAgentydragon = readKey "wyrm2-mcp-agentydragon.pub";
  wyrm2McpRoot = readKey "wyrm2-mcp-root.pub";
  ruggedMcpAgentydragon = readKey "rugged-mcp-agentydragon.pub";
  ruggedMcpRoot = readKey "rugged-mcp-root.pub";
}
