# httpx2: the pydantic org's next-generation HTTP client (mcp-sdk v2 / fastmcp v4's
# unconditional base dependency, replacing plain httpx for their own internal use).
# Not in nixpkgs as of the pinned nixpkgs revision (first released 2026-05, after the pin).
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "httpx2";
  version = "2.12.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/c8/95/411ba65569158e862368917aaf56597f3e5fa3b91b0502919638465a08f3/httpx2-2.12.0-py3-none-any.whl";
    hash = "sha256-zItu7LhmHBRrj4mmDpdFbuCG6Rp4TtMaxFDDqeYT3TY=";
  };

  dependencies = with python3Packages; [
    anyio
    httpcore2
    idna
    truststore
  ];

  doCheck = false;

  pythonImportsCheck = [ "httpx2" ];

  meta = {
    description = "The next generation HTTP client";
    homepage = "https://github.com/pydantic/httpx2";
    license = lib.licenses.bsd3;
  };
}
