{ pkgs }:

let
  src = pkgs.fetchFromGitHub {
    owner = "zhaofengli";
    repo = "attic";
    rev = "295a33a1e4a5138868e6f45dc75ed68a05c0300f";
    hash = "sha256-I2cjqxsVyoqAKl1/egoIDV5h96ACOdf9RQZz+2dBvAk=";
  };
  version = "0-unstable-2026-06-11";
  cargoHash = "sha256-LqE4jOIasxIG4DAhgZJMlTSyt/a900QR06wBFtRNRO8=";
in

pkgs.attic-server.overrideAttrs (old: {
  inherit src version cargoHash;
  cargoDeps = pkgs.rustPlatform.fetchCargoVendor {
    pname = "attic";
    inherit src version;
    hash = cargoHash;
  };
  patches = (old.patches or [ ]) ++ [ ./database-max-connections.patch ];
  doCheck = true;
  cargoTestFlags = [
    "-p"
    "attic-server"
    "--lib"
    "database_pool_tests::database_max_connections_configures_the_sqlite_pool"
  ];
})
