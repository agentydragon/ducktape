# asyncvnc: Async VNC client library
# Not in nixpkgs, used by hetzner-vnc-screenshot
{
  lib,
  python3Packages,
  fetchurl,
  keysymdef,
}:
python3Packages.buildPythonPackage rec {
  pname = "asyncvnc";
  version = "1.3.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/5b/55/e7c4483b8952bbe048a107d85a29f06a6336e302b79a2e0508d61d926e43/asyncvnc-1.3.0-py3-none-any.whl";
    hash = "sha256-9N5OhYRJMlrvz4KkazS++zSv3YEeIOevmwRAGS5JbnQ=";
  };

  dependencies = [
    keysymdef
    python3Packages.cryptography
    python3Packages.numpy
  ];

  doCheck = false;

  pythonImportsCheck = [ "asyncvnc" ];

  meta = {
    description = "Async VNC client library";
    homepage = "https://github.com/barneygale/asyncvnc";
    license = lib.licenses.mit;
  };
}
