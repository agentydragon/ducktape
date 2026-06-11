# keysymdef: X11 key symbol definitions for Python
# Not in nixpkgs, dependency of asyncvnc
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "keysymdef";
  version = "1.2.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/42/d3/c3db0b92a0ff39c3e08f168cd382c24bf021d4a96fc89b47a3e55294f883/keysymdef-1.2.0-py2.py3-none-any.whl";
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
