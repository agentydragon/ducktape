# httpcore2: the low-level transport for httpx2 (pydantic org's http2/httpx successor).
# The pinned nixpkgs package is older than httpx2's required 2.16.0 transport.
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "httpcore2";
  version = "2.16.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/af/c2/ce417c397afa5181f0e3c6ff01a4d01639414e1adef08730d97571297151/httpcore2-2.16.0-py3-none-any.whl";
    hash = "sha256-XENYMZ8mAtmErZMYLJ/uUWDyLp2nur3ahwpefE4tw08=";
  };

  dependencies = with python3Packages; [
    h11
    truststore
  ];

  doCheck = false;

  pythonImportsCheck = [ "httpcore2" ];

  meta = {
    description = "The next generation HTTP transport library (httpx2's backend)";
    homepage = "https://github.com/pydantic/httpx2";
    license = lib.licenses.bsd3;
  };
}
