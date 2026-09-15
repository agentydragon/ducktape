# httpx2: the pydantic org's next-generation HTTP client (mcp-sdk v2 / fastmcp v4's
# unconditional base dependency, replacing plain httpx for their own internal use).
# The pinned nixpkgs package is older than the >=2.13.0 floor required here.
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "httpx2";
  version = "2.13.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/fe/d1/a0c72b0e006df654709fbc366cc5bcb53e5aee13e1e3395152c6dd293376/httpx2-2.13.0-py3-none-any.whl";
    hash = "sha256-/BJyDO33L6omzKa0yjlOBciU59eTP8Rcr+dnlggE5Jo=";
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
