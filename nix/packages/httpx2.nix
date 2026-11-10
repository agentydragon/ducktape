# httpx2: the pydantic org's next-generation HTTP client (mcp-sdk v2 / fastmcp v4's
# unconditional base dependency, replacing plain httpx for their own internal use).
# The pinned nixpkgs package is older than the >=2.16.0 floor required here.
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "httpx2";
  version = "2.16.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/7b/3d/2e4bc4fd1ef9770871669adb348d612a6e4786b303a23f9935b81c48dd60/httpx2-2.16.0-py3-none-any.whl";
    hash = "sha256-/SxdeQno2G/pLVMCDDEMoOTkMbg4wgfFdUubvwMI1Jo=";
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
