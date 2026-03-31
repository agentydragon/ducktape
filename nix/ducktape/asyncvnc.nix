# asyncvnc: Async VNC client library
# Not in nixpkgs, used by hetzner-vnc-screenshot
{
  lib,
  python3Packages,
  fetchPypi,
  keysymdef,
}:
python3Packages.buildPythonPackage rec {
  pname = "asyncvnc";
  version = "1.3.0";
  format = "wheel";

  src = fetchPypi {
    inherit pname version;
    format = "wheel";
    python = "py3";
    abi = "none";
    platform = "any";
    hash = "sha256-9N5OhYRJMlrvz4KkazS++zSv3YEeIOevmwRAGS5JbnQ=";
  };

  dependencies = [ keysymdef ];

  doCheck = false;

  pythonImportsCheck = [ "asyncvnc" ];

  meta = {
    description = "Async VNC client library";
    homepage = "https://github.com/barneygale/asyncvnc";
    license = lib.licenses.mit;
  };
}
