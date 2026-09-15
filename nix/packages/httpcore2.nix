# httpcore2: the low-level transport for httpx2 (pydantic org's http2/httpx successor).
# The pinned nixpkgs package is older than httpx2's required 2.13.0 transport.
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "httpcore2";
  version = "2.13.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/7e/0d/117a771a2bb91df334b66bf4da14cd02f21aefbcfe53180f336ce55e8f90/httpcore2-2.13.0-py3-none-any.whl";
    hash = "sha256-Na5b40eqQEZ7Sl3AMqxn67bScYn8l+jOvPmWFvahu54=";
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
