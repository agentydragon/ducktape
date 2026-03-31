# keysymdef: X11 key symbol definitions for Python
# Not in nixpkgs, dependency of asyncvnc
{
  lib,
  python3Packages,
  fetchPypi,
}:
python3Packages.buildPythonPackage rec {
  pname = "keysymdef";
  version = "1.2.0";
  format = "wheel";

  src = fetchPypi {
    inherit pname version;
    format = "wheel";
    python = "py3";
    abi = "none";
    platform = "any";
    hash = "sha256-GaXCJjqGHz/4hKH1jitPfvoxn/ydEfm6jiASm6vDGp4=";
  };

  doCheck = false;

  pythonImportsCheck = [ "keysymdef" ];

  meta = {
    description = "X11 key symbol definitions";
    homepage = "https://github.com/nickcoutsos/python-keysymdef";
    license = lib.licenses.mit;
  };
}
