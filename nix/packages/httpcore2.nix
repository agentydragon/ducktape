# httpcore2: the low-level transport for httpx2 (pydantic org's http2/httpx successor).
# Not in nixpkgs as of the pinned nixpkgs revision (first released 2026-05, after the pin).
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "httpcore2";
  version = "2.12.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/d2/74/d370e55600d9bcfa0d9794b0166126d49291a3d2b20c268fc98c453a4948/httpcore2-2.12.0-py3-none-any.whl";
    hash = "sha256-fgQljOAQE9fWFeW5EKOyf6yTfXqVA4In55ZStLo7TOs=";
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
