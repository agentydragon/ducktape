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
}
